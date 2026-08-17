"""Typed parsing of OFIQSampleApp standard output.

OFIQSampleApp (v1.1.x) prints, in order: an initialization line, the engine
version line, one semicolon-separated header row, and one result row per input
image. Each row carries every quality measure twice, ``<Measure>.native`` (the
raw engine value) and ``<Measure>.scalar`` (the ISO/IEC 29794-5 quality
component in [0, 100]), followed by ``assessment_time_in_ms``.

Two engine behaviours are surfaced explicitly rather than smoothed over:

* a measure that the engine could not compute is reported by the engine as
  scalar ``-1`` (and a non-finite or sentinel native value); it is kept as an
  explicit per-measure failure, never as a number;
* an image the engine could not read produces a ``[ERROR]`` line on stderr and
  a result row whose measure cells are empty; it becomes an explicit per-row
  failure and never a fabricated assessment.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import math
import re

INITIALIZATION_RE = re.compile(r"^\[INFO\] Initialization took: ([0-9]+)ms$")
VERSION_RE = re.compile(r"^OFIQ library version: (\S+)$")
HEADER_PREFIX = "Filename;"
ASSESSMENT_TIME_COLUMN = "assessment_time_in_ms"
NATIVE_SUFFIX = ".native"
SCALAR_SUFFIX = ".scalar"
ENGINE_FAILURE_SCALAR = -1


class OFIQOutputError(ValueError):
    """Raised when OFIQSampleApp output cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class OFIQMeasure:
    """One ISO/IEC 29794-5 measure as reported by the engine for one image."""

    name: str
    native: float | None
    scalar: int | None
    failed: bool

    @property
    def native_is_finite(self) -> bool:
        return self.native is not None and math.isfinite(self.native)


@dataclass(frozen=True, slots=True)
class OFIQRow:
    """The engine's result for one input path."""

    filename: str
    measures: tuple[OFIQMeasure, ...]
    assessment_ms: int
    row_failed: bool
    failure_reason: str | None

    def measure(self, name: str) -> OFIQMeasure:
        for item in self.measures:
            if item.name == name:
                return item
        raise KeyError(name)

    @property
    def failed_measure_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.measures if item.failed)


@dataclass(frozen=True, slots=True)
class OFIQAssessment:
    """Everything OFIQSampleApp reported for one invocation."""

    engine_version: str
    initialization_ms: int
    measure_names: tuple[str, ...]
    rows: tuple[OFIQRow, ...]

    def row_for(self, filename: str) -> OFIQRow:
        for row in self.rows:
            if row.filename == filename:
                return row
        raise KeyError(filename)


def _parse_native(text: str) -> tuple[float | None, bool]:
    stripped = text.strip()
    if not stripped:
        return None, True
    try:
        value = float(stripped)
    except ValueError as error:
        raise OFIQOutputError(f"native value {stripped!r} is not numeric") from error
    if not math.isfinite(value):
        return None, True
    return value, False


def _parse_scalar(text: str) -> tuple[int | None, bool]:
    stripped = text.strip()
    if not stripped:
        return None, True
    try:
        value = float(stripped)
    except ValueError as error:
        raise OFIQOutputError(f"scalar value {stripped!r} is not numeric") from error
    if not math.isfinite(value) or not value.is_integer():
        raise OFIQOutputError(f"scalar value {stripped!r} is not an integer quality component")
    scalar = int(value)
    if scalar == ENGINE_FAILURE_SCALAR:
        return None, True
    if scalar < 0 or scalar > 100:
        raise OFIQOutputError(f"scalar value {scalar} is outside the [0, 100] component range")
    return scalar, False


def parse_ofiq_stdout(stdout: str, stderr: str = "") -> OFIQAssessment:
    """Parse OFIQSampleApp stdout (and stderr for row-level errors) into typed values."""

    lines = stdout.splitlines()
    initialization = None
    version = None
    header_index = None
    for index, line in enumerate(lines):
        if initialization is None and (match := INITIALIZATION_RE.fullmatch(line)):
            initialization = int(match.group(1))
        elif version is None and (match := VERSION_RE.fullmatch(line)):
            version = match.group(1)
        elif header_index is None and line.startswith(HEADER_PREFIX):
            header_index = index
            break
    if initialization is None:
        raise OFIQOutputError("initialization line is missing")
    if version is None:
        raise OFIQOutputError("engine version line is missing")
    if header_index is None:
        raise OFIQOutputError("result header row is missing")

    table_lines = [line for line in lines[header_index:] if line]
    parsed = list(csv.reader(table_lines, delimiter=";", strict=True))
    header = parsed[0]
    while header and header[-1] == "":
        header.pop()
    if not header or header[0] != "Filename" or header[-1] != ASSESSMENT_TIME_COLUMN:
        raise OFIQOutputError("result header must start with Filename and end with assessment_time_in_ms")
    measure_columns = header[1:-1]
    natives = [name for name in measure_columns if name.endswith(NATIVE_SUFFIX)]
    scalars = [name for name in measure_columns if name.endswith(SCALAR_SUFFIX)]
    if len(natives) + len(scalars) != len(measure_columns):
        raise OFIQOutputError("result header contains a column that is neither native nor scalar")
    measure_names = tuple(name[: -len(NATIVE_SUFFIX)] for name in natives)
    if tuple(name[: -len(SCALAR_SUFFIX)] for name in scalars) != measure_names:
        raise OFIQOutputError("native and scalar measure columns do not correspond")
    if len(measure_names) != len(set(measure_names)):
        raise OFIQOutputError("result header contains duplicate measure names")

    error_lines = [line for line in stderr.splitlines() if line.startswith("[ERROR]")]
    rows: list[OFIQRow] = []
    for values in parsed[1:]:
        while values and values[-1] == "":
            values.pop()
        if not values:
            continue
        filename = values[0]
        try:
            assessment_ms = int(values[-1])
        except ValueError as error:
            raise OFIQOutputError(f"assessment time {values[-1]!r} is not an integer") from error
        cells = values[1:-1]
        if not measure_names and not cells:
            # The engine could not read the first image, so its header carries
            # no measure names; every row in this invocation is unassessable.
            rows.append(
                OFIQRow(
                    filename=filename,
                    measures=(),
                    assessment_ms=assessment_ms,
                    row_failed=True,
                    failure_reason=(
                        error_lines.pop(0) if error_lines else "engine reported no measures for this image"
                    ),
                )
            )
            continue
        if len(cells) != 2 * len(measure_names):
            if all(cell == "" for cell in cells):
                rows.append(
                    OFIQRow(
                        filename=filename,
                        measures=(),
                        assessment_ms=assessment_ms,
                        row_failed=True,
                        failure_reason=(
                            error_lines.pop(0) if error_lines else "engine reported empty measures for this image"
                        ),
                    )
                )
                continue
            raise OFIQOutputError(
                f"row for {filename} has {len(cells)} measure cells, expected {2 * len(measure_names)}"
            )
        native_cells = cells[: len(measure_names)]
        scalar_cells = cells[len(measure_names) :]
        measures: list[OFIQMeasure] = []
        for name, native_text, scalar_text in zip(measure_names, native_cells, scalar_cells, strict=True):
            native, native_failed = _parse_native(native_text)
            scalar, scalar_failed = _parse_scalar(scalar_text)
            failed = native_failed or scalar_failed
            measures.append(
                OFIQMeasure(
                    name=name,
                    native=native if not native_failed else None,
                    scalar=scalar if not scalar_failed else None,
                    failed=failed,
                )
            )
        rows.append(
            OFIQRow(
                filename=filename,
                measures=tuple(measures),
                assessment_ms=assessment_ms,
                row_failed=False,
                failure_reason=None,
            )
        )
    if not rows:
        raise OFIQOutputError("result table has no rows")
    return OFIQAssessment(
        engine_version=version,
        initialization_ms=initialization,
        measure_names=measure_names,
        rows=tuple(rows),
    )
