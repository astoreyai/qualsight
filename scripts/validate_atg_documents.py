#!/usr/bin/env python3
"""Validate QualSight's real requirements, ATG documents, and ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRD_PATH = PROJECT_ROOT / "PRD.md"
PLAN_PATH = PROJECT_ROOT / "PLAN.md"
REQUIREMENTS_PATH = PROJECT_ROOT / "REQUIREMENTS.md"
ATG_PLAN_PATH = PROJECT_ROOT / "ATG_PLAN.md"
ATG_LOOP_PATH = PROJECT_ROOT / "ATG_LOOP.md"
LEDGER_PATH = PROJECT_ROOT / "atg/ledger.jsonl"
AUTHORIZATION_PATH = (
    PROJECT_ROOT
    / "resources/authorizations/qualsight-local-noncommercial-research-2026-08-14.json"
)

REQUIREMENT_RE = re.compile(r"^QS-REQ-[0-9]{3}[A-Z]?$")
DECISION_RE = re.compile(r"^QS-SRC-[0-9]{3}$")
CAPABILITY_RE = re.compile(r"^QS-[A-Z][0-9]{2}$")
ATG_NODE_REFERENCE_RE = re.compile(r"QS-[A-Z][0-9]{2}(?:\.[A-Z0-9]+)*")
PRD_HEADING_RE = re.compile(r"^# ([0-9]+)\.", re.MULTILINE)
SOURCE_LINK_RE = re.compile(r"^\[PRD\.md:([0-9]+)(?:[–-]([0-9]+))?\]\(\./PRD\.md\)$")
FORBIDDEN_MARKER_RE = re.compile(
    r"\b(?:TODO|FIXME|TBD)\b|\[date to be inserted\]|\bN records\b|\bX met\b",
    re.IGNORECASE,
)
ALLOWED_CLASSES = {"REQUIRED_BETA", "CONDITIONAL", "DEFERRED", "POST_BETA"}
ALLOWED_LEDGER_STATES = {
    "ACCEPTED",
    "BLOCKED",
    "FAILED",
    "READY",
    "RUNNING",
    "SUPERSEDED",
}
LEDGER_REQUIRED_FIELDS = {
    "schema_version",
    "cycle_id",
    "iteration_index",
    "node_id",
    "historical_ancestor_id",
    "timestamp",
    "operator",
    "input_hashes",
    "action",
    "output_artifacts",
    "exit_code",
    "baseline",
    "final",
    "guard",
    "progress",
    "status",
    "error_classification",
    "minimal_repair_subgraph",
    "source_commit",
    "dirty_state",
    "runtime_observation",
    "unresolved_external_blocks",
    "rollback",
}
CURRENT_ACCEPTED_WHOLE = {
    "QS-A00",
    "QS-A01",
    "QS-A02",
    "QS-A03",
    "QS-A04",
    "QS-A05",
    "QS-A06",
    "QS-A07",
    "QS-A08",
    "QS-A09",
    "QS-A10",
    "QS-B10",
    "QS-B11",
    "QS-B12",
    "QS-B13",
    "QS-B14",
    "QS-C20",
    "QS-C22",
    "QS-C23",
    "QS-C24",
    "QS-C25",
    "QS-D30",
    "QS-D31",
    "QS-D32",
    "QS-D33",
    "QS-D34",
    "QS-E40",
    "QS-E41",
    "QS-E42",
    "QS-E43",
    "QS-F50",
    "QS-F51",
    "QS-F52",
    "QS-F53",
    "QS-F54",
    "QS-H70",
    "QS-H71",
    "QS-H72",
    "QS-H73",
}
CURRENT_ACCEPTED_REFINEMENTS = {"QS-A00.R8", "QS-A05.R1"}
CURRENT_ACCEPTED_CHILDREN = {
    "QS-A02.R7",
    "QS-A02.LFW.R0",
    "QS-A02.LFW.P0",
    "QS-A02.SCRFD.P0",
    "QS-A03.R1",
    "QS-A04.R1",
    "QS-A04.R2",
    "QS-A06.R1O",
    "QS-A06.R2",
    "QS-B12.LFW.FULL",
    "QS-C22.CROP.R0",
    "QS-C22.MATRIX.LFW",
    "QS-D31.R0",
}
SUPERSEDED_HISTORICAL_CHILDREN = {
    "QS-A02.R0",
    "QS-A02.R1",
    "QS-A02.R2",
    "QS-A02.R3",
    "QS-A02.R4",
    "QS-A02.R5",
    "QS-A02.R6",
    "QS-A03.R0",
    "QS-A04.R0",
    "QS-A06.R0",
    "QS-A06.R1M",
}
BLOCKED_PARENT_CAPABILITIES = {"QS-C21"}
BLOCKED_REFINEMENT_PARENTS: set[str] = set()
ACCEPTED_REFINEMENT_PARENTS = {"QS-A02.LFW"}
CURRENT_OUTPUT_AUTHORITY_NODE = "QS-B14"
LEDGER_FROZEN_PREFIX_RECORDS = 113
LEDGER_FROZEN_PREFIX_SHA256 = (
    "475ae6dbad7d77369878c4784caf2f192c14b1addf712c6fa40bde822e1f18fd"
)
PREPROCESSING_CONDITIONS = {
    "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
    "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
}
MATCHER_TRANSFORMS = {
    "ADAFACE_IR101_BGR_NEG1_POS1_L2_V1",
    "ELASTICFACE_ARC_R100_RGB_NEG1_POS1_L2_V1",
    "TRANSFACE_S_P9_RGB_NEG1_POS1_L2_V1",
}
MATCHER_CONDITION_LEAVES = {
    "QS-C22.AS",
    "QS-C22.AN",
    "QS-C22.ES",
    "QS-C22.EN",
    "QS-C22.TS",
    "QS-C22.TN",
}


class ATGDocumentValidationError(ValueError):
    """Raised when the current ATG document set is internally inconsistent."""


def _fail(location: str, message: str) -> None:
    raise ATGDocumentValidationError(f"{location}: {message}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ATGDocumentValidationError(f"ledger: duplicate JSON key {key}")
        value[key] = item
    return value


def _table_parts(line: str) -> list[str]:
    return [part.strip() for part in line.split("|")[1:-1]]


def _validate_frozen_sources(requirements_text: str) -> None:
    start = requirements_text.index("## Frozen source snapshot")
    end = requirements_text.index("## Classification", start)
    snapshot = requirements_text[start:end]
    expected_paths = {
        "PRD.md": PRD_PATH,
        "PLAN.md": PLAN_PATH,
        "Owner authorization receipt": AUTHORIZATION_PATH,
    }
    observed: dict[str, str] = {}
    for line in snapshot.splitlines():
        parts = _table_parts(line) if line.startswith("|") else []
        if len(parts) == 3 and parts[0] in expected_paths:
            observed[parts[0]] = parts[1]
    if set(observed) != set(expected_paths):
        _fail("REQUIREMENTS.md frozen sources", "must name PRD, PLAN, and owner authorization")
    for label, path in expected_paths.items():
        actual = _sha256(path)
        if observed[label] != actual:
            _fail("REQUIREMENTS.md frozen sources", f"{label} SHA-256 drift: {actual}")


def _prd_section_bounds(prd_text: str) -> dict[int, tuple[int, int]]:
    starts = [
        (int(match.group(1)), prd_text.count("\n", 0, match.start()) + 1)
        for match in PRD_HEADING_RE.finditer(prd_text)
    ]
    if [section for section, _line in starts] != list(range(1, 61)):
        _fail("PRD.md", "numbered sections must be exactly 1 through 60")
    total_lines = len(prd_text.splitlines())
    return {
        section: (line, starts[index + 1][1] - 1 if index + 1 < len(starts) else total_lines)
        for index, (section, line) in enumerate(starts)
    }


def _validate_requirements(
    requirements_text: str,
    prd_text: str,
    known_nodes: set[str],
) -> tuple[int, int, int]:
    rows: list[tuple[str, int, int, int, str, str]] = []
    decisions: list[tuple[str, str]] = []
    for line_number, line in enumerate(requirements_text.splitlines(), start=1):
        if line.startswith("| QS-REQ-"):
            parts = _table_parts(line)
            if len(parts) != 7:
                _fail(f"REQUIREMENTS.md:{line_number}", "requirement row must have seven columns")
            requirement_id, section_text, source, classification, _text, owner, _evidence = parts
            if not REQUIREMENT_RE.fullmatch(requirement_id):
                _fail(f"REQUIREMENTS.md:{line_number}", f"invalid requirement ID {requirement_id}")
            if classification not in ALLOWED_CLASSES:
                _fail(f"REQUIREMENTS.md:{line_number}", f"invalid class {classification}")
            if owner not in known_nodes:
                _fail(f"REQUIREMENTS.md:{line_number}", f"unknown owner {owner}")
            source_match = SOURCE_LINK_RE.fullmatch(source)
            if source_match is None:
                _fail(f"REQUIREMENTS.md:{line_number}", "invalid PRD source link")
            start = int(source_match.group(1))
            end = int(source_match.group(2) or source_match.group(1))
            rows.append((requirement_id, int(section_text), start, end, classification, owner))
        elif line.startswith("| QS-SRC-"):
            parts = _table_parts(line)
            if len(parts) != 4:
                _fail(f"REQUIREMENTS.md:{line_number}", "decision row must have four columns")
            decision_id, _source, _decision, owner = parts
            if not DECISION_RE.fullmatch(decision_id):
                _fail(f"REQUIREMENTS.md:{line_number}", f"invalid decision ID {decision_id}")
            if owner not in known_nodes:
                _fail(f"REQUIREMENTS.md:{line_number}", f"unknown decision owner {owner}")
            decisions.append((decision_id, owner))
    ids = [row[0] for row in rows]
    if len(rows) != 81 or len(ids) != len(set(ids)):
        _fail("REQUIREMENTS.md", "must contain 81 unique requirement IDs")
    decision_ids = [item[0] for item in decisions]
    if len(decisions) != 12 or len(decision_ids) != len(set(decision_ids)):
        _fail("REQUIREMENTS.md", "must contain 12 unique decision/source IDs")
    bounds = _prd_section_bounds(prd_text)
    covered: set[int] = set()
    for requirement_id, section, start, end, _classification, _owner in rows:
        if section not in bounds:
            _fail(requirement_id, f"unknown PRD section {section}")
        section_start, section_end = bounds[section]
        if start > end or start < section_start or end > section_end:
            _fail(requirement_id, f"source lines {start}-{end} escape PRD section {section}")
        covered.add(section)
    if covered != set(range(1, 61)):
        _fail("REQUIREMENTS.md", "requirements must cover all 60 PRD sections")
    return len(rows), len(covered), len(decisions)


def _capability_graph(plan_text: str) -> tuple[set[str], set[tuple[str, str]]]:
    section_start = plan_text.index("## Level 2 — Capability DAG")
    fence_start = plan_text.index("~~~mermaid", section_start) + len("~~~mermaid")
    fence_end = plan_text.index("~~~", fence_start)
    graph = plan_text[fence_start:fence_end]
    aliases = set(re.findall(r"\b([A-Z][0-9]{2})\[", graph))
    edges = set(re.findall(r"\b([A-Z][0-9]{2})\s*-->\s*([A-Z][0-9]{2})", graph))
    if len(aliases) != 41:
        _fail("ATG_PLAN.md capability DAG", f"expected 41 nodes, observed {len(aliases)}")
    if len(edges) != 85:
        _fail("ATG_PLAN.md capability DAG", f"expected 85 edges, observed {len(edges)}")
    if any(source not in aliases or target not in aliases for source, target in edges):
        _fail("ATG_PLAN.md capability DAG", "edge references an undeclared node")
    required_repaired_edges = {
        ("A02", "A03"),
        ("A06", "A03"),
        ("C20", "C22"),
    }
    if (
        not required_repaired_edges.issubset(edges)
        or ("C21", "E40") in edges
        or ("C21", "C25") in edges
        or ("C21", "C23") in edges
        or ("C21", "H74") not in edges
    ):
        _fail("ATG_PLAN.md capability DAG", "required dependency repair drift")
    incoming = {node: 0 for node in aliases}
    outgoing = {node: set() for node in aliases}
    for source, target in edges:
        outgoing[source].add(target)
        incoming[target] += 1
    ready = [node for node, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for target in outgoing[node]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(aliases):
        _fail("ATG_PLAN.md capability DAG", "graph contains a cycle")
    return aliases, edges


def _validate_capability_table(
    plan_text: str,
    aliases: set[str],
    graph_edges: set[tuple[str, str]],
) -> dict[str, str]:
    start = plan_text.index("## Capability registry")
    end = plan_text.index("## Post-beta branch", start)
    states: dict[str, str] = {}
    table_edges: set[tuple[str, str]] = set()
    for line_number, line in enumerate(
        plan_text[start:end].splitlines(),
        start=plan_text[:start].count("\n") + 1,
    ):
        if not line.startswith("| QS-"):
            continue
        parts = _table_parts(line)
        if len(parts) != 6 or not CAPABILITY_RE.fullmatch(parts[0]):
            _fail(f"ATG_PLAN.md:{line_number}", "invalid capability table row")
        node_id = parts[0]
        if node_id in states:
            _fail(f"ATG_PLAN.md:{line_number}", f"duplicate capability {node_id}")
        states[node_id] = parts[5]
        dependencies = () if parts[4] == "none" else tuple(parts[4].split(", "))
        for dependency in dependencies:
            if not CAPABILITY_RE.fullmatch(dependency):
                _fail(
                    f"ATG_PLAN.md:{line_number}",
                    f"invalid dependency {dependency} for {node_id}",
                )
            table_edges.add((dependency.removeprefix("QS-"), node_id.removeprefix("QS-")))
    table_nodes = set(states)
    expected = {f"QS-{alias}" for alias in aliases}
    if table_nodes != expected:
        _fail("ATG_PLAN.md capability registry", "table and Mermaid nodes differ")
    if table_edges != graph_edges:
        missing = sorted(graph_edges - table_edges)
        extra = sorted(table_edges - graph_edges)
        _fail(
            "ATG_PLAN.md capability registry",
            f"table dependencies differ from Mermaid edges; missing={missing}, extra={extra}",
        )
    accepted = {node_id for node_id, state in states.items() if state == "ACCEPTED"}
    if accepted != CURRENT_ACCEPTED_WHOLE:
        _fail(
            "ATG_PLAN.md capability registry",
            f"accepted whole-capability set differs: {sorted(accepted)}",
        )
    for node_id in BLOCKED_PARENT_CAPABILITIES:
        if not states[node_id].startswith("BLOCKED"):
            _fail(
                "ATG_PLAN.md capability registry",
                f"blocked parent {node_id} has state {states[node_id]}",
            )
    return states


def _validate_matcher_matrix(plan_text: str) -> tuple[int, int, int]:
    start = plan_text.index("### Frozen three-matcher, two-preprocessing matrix")
    end = plan_text.index("## Level 0 — Product graph interface", start)
    matrix = plan_text[start:end]
    transform_start = matrix.index("| Matcher transform ID")
    condition_text = matrix[:transform_start]
    transform_text = matrix[transform_start:]
    conditions = set(
        re.findall(r"^\| `([A-Z0-9_]+)` \|", condition_text, re.MULTILINE)
    )
    transforms = set(
        re.findall(r"^\| `([A-Z0-9_]+)` \|", transform_text, re.MULTILINE)
    )
    leaves = set(re.findall(r"`(QS-C22\.[A-Z]{2})`", matrix))
    if conditions != PREPROCESSING_CONDITIONS:
        _fail(
            "ATG_PLAN.md matcher matrix",
            f"preprocessing conditions differ: {sorted(conditions)}",
        )
    if transforms != MATCHER_TRANSFORMS:
        _fail(
            "ATG_PLAN.md matcher matrix",
            f"matcher transforms differ: {sorted(transforms)}",
        )
    if leaves != MATCHER_CONDITION_LEAVES:
        _fail(
            "ATG_PLAN.md matcher matrix",
            f"matcher-condition leaves differ: {sorted(leaves)}",
        )
    return len(transforms), len(conditions), len(leaves)


def _validate_controller_snapshot(
    plan_text: str,
    loop_text: str,
) -> dict[str, set[str]]:
    start = loop_text.index("## v1.6 controller snapshot")
    end = loop_text.index(
        "The historical atomic `A05` repository-initialization leaf", start
    )
    rows: dict[str, tuple[int, set[str]]] = {}
    for line in loop_text[start:end].splitlines():
        if not line.startswith("|"):
            continue
        parts = _table_parts(line)
        if len(parts) != 4:
            continue
        if parts[1].isdigit():
            rows[parts[0]] = (
                int(parts[1]),
                set(ATG_NODE_REFERENCE_RE.findall(parts[2])),
            )
        elif parts[0] == "Authorized OFIQ process executions":
            rows[parts[0]] = (-1, set(ATG_NODE_REFERENCE_RE.findall(parts[2])))
    expected_groups = {
        "Accepted whole capabilities": CURRENT_ACCEPTED_WHOLE,
        "Current accepted evidence children": CURRENT_ACCEPTED_CHILDREN,
        "Superseded historical child events": SUPERSEDED_HISTORICAL_CHILDREN,
        "Blocked parent capabilities": BLOCKED_PARENT_CAPABILITIES,
        "Blocked refinement parents": BLOCKED_REFINEMENT_PARENTS,
        "Accepted refinement parents": ACCEPTED_REFINEMENT_PARENTS,
    }
    for label, expected in expected_groups.items():
        if label not in rows:
            _fail("ATG_LOOP.md controller snapshot", f"missing row {label}")
        count, observed = rows[label]
        if count != len(observed) or observed != expected:
            _fail(
                "ATG_LOOP.md controller snapshot",
                f"{label} differs: count={count}, ids={sorted(observed)}",
            )
    if rows.get("Dependency-ready whole capabilities") != (0, set()):
        _fail("ATG_LOOP.md controller snapshot", "no whole capability is dependency-ready while QS-H74 waits for QS-C21")
    if "Authorized OFIQ process executions" not in rows:
        _fail("ATG_LOOP.md controller snapshot", "authorized OFIQ execution row is missing")

    active_match = re.search(
        r"active accepted evidence set is `\{([^}]*)\}`",
        plan_text,
    )
    if active_match is None:
        _fail("ATG_PLAN.md current frontier", "active accepted evidence set is absent")
    active = set(
        ATG_NODE_REFERENCE_RE.findall(active_match.group(1))
    )
    expected_active = CURRENT_ACCEPTED_WHOLE | CURRENT_ACCEPTED_CHILDREN
    if active != expected_active:
        _fail(
            "ATG_PLAN.md current frontier",
            f"active accepted set differs: {sorted(active)}",
        )
    for node_id in sorted(CURRENT_ACCEPTED_REFINEMENTS):
        if re.search(rf"\*\*State:\*\* ACCEPTED[^\n]*{re.escape(node_id)}", plan_text) is None:
            _fail(
                "ATG_PLAN.md current frontier",
                f"accepted refinement claim {node_id} is absent",
            )
    return {label: value[1] for label, value in rows.items()}


def _validate_ledger() -> tuple[int, list[dict[str, Any]], int]:
    seen_iterations: set[tuple[str, int]] = set()
    records: list[dict[str, Any]] = []
    count = 0
    raw_ledger = LEDGER_PATH.read_bytes()
    try:
        ledger_text = raw_ledger.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail("atg/ledger.jsonl", f"ledger is not UTF-8: {error}")
    raw_lines = raw_ledger.splitlines(keepends=True)
    if len(raw_lines) < LEDGER_FROZEN_PREFIX_RECORDS:
        _fail("atg/ledger.jsonl", "frozen historical ledger records were removed")
    historical_prefix = b"".join(raw_lines[:LEDGER_FROZEN_PREFIX_RECORDS])
    historical_digest = hashlib.sha256(historical_prefix).hexdigest()
    if historical_digest != LEDGER_FROZEN_PREFIX_SHA256:
        _fail(
            "atg/ledger.jsonl",
            f"first {LEDGER_FROZEN_PREFIX_RECORDS} records drifted: {historical_digest}",
        )
    for line_number, line in enumerate(ledger_text.splitlines(), start=1):
        if not line:
            _fail(f"atg/ledger.jsonl:{line_number}", "blank lines are not allowed")
        try:
            record = json.loads(line, object_pairs_hook=_no_duplicate_keys)
        except json.JSONDecodeError as error:
            _fail(f"atg/ledger.jsonl:{line_number}", f"invalid JSON: {error}")
        if not isinstance(record, dict):
            _fail(f"atg/ledger.jsonl:{line_number}", "record must be an object")
        missing = LEDGER_REQUIRED_FIELDS - set(record)
        if missing:
            _fail(f"atg/ledger.jsonl:{line_number}", f"missing fields: {', '.join(sorted(missing))}")
        if record["schema_version"] != "1.0":
            _fail(f"atg/ledger.jsonl:{line_number}", "unsupported schema version")
        if record["status"] not in ALLOWED_LEDGER_STATES:
            _fail(f"atg/ledger.jsonl:{line_number}", f"invalid state {record['status']}")
        if (
            not isinstance(record["cycle_id"], str)
            or not record["cycle_id"]
            or not isinstance(record["iteration_index"], int)
            or isinstance(record["iteration_index"], bool)
            or record["iteration_index"] < 1
            or not isinstance(record["node_id"], str)
            or not record["node_id"]
            or not isinstance(record["historical_ancestor_id"], str)
            or not record["historical_ancestor_id"]
        ):
            _fail(f"atg/ledger.jsonl:{line_number}", "invalid cycle or node identity")
        key = (record["cycle_id"], record["iteration_index"])
        if key in seen_iterations:
            _fail(f"atg/ledger.jsonl:{line_number}", "duplicate cycle iteration")
        seen_iterations.add(key)
        records.append(record)
        count += 1
    return count, records, LEDGER_FROZEN_PREFIX_RECORDS


def _latest_ledger_status(
    records: list[dict[str, Any]],
    node_id: str,
    *,
    include_descendants: bool,
) -> str | None:
    matches = []
    for record in records:
        recorded_node = record["node_id"]
        exact_or_version = recorded_node == node_id or recorded_node.startswith(
            f"{node_id}.V"
        )
        descendant = include_descendants and record["historical_ancestor_id"] == node_id
        if exact_or_version or descendant:
            matches.append(record)
    return matches[-1]["status"] if matches else None


def _validate_ledger_state_claims(records: list[dict[str, Any]]) -> int:
    for node_id in sorted(CURRENT_ACCEPTED_WHOLE):
        status = _latest_ledger_status(records, node_id, include_descendants=True)
        if status != "ACCEPTED":
            _fail(
                "atg/ledger.jsonl",
                f"accepted whole capability {node_id} has latest ledger state {status}",
            )
    for node_id in sorted(CURRENT_ACCEPTED_REFINEMENTS | CURRENT_ACCEPTED_CHILDREN):
        status = _latest_ledger_status(records, node_id, include_descendants=False)
        if status != "ACCEPTED":
            _fail(
                "atg/ledger.jsonl",
                f"current accepted child {node_id} has latest ledger state {status}",
            )
    for node_id in sorted(SUPERSEDED_HISTORICAL_CHILDREN):
        status = _latest_ledger_status(records, node_id, include_descendants=False)
        if status != "SUPERSEDED":
            _fail(
                "atg/ledger.jsonl",
                f"historical child {node_id} has latest ledger state {status}",
            )
    for node_id in sorted(BLOCKED_PARENT_CAPABILITIES):
        status = _latest_ledger_status(records, node_id, include_descendants=False)
        if status != "BLOCKED":
            _fail(
                "atg/ledger.jsonl",
                f"blocked parent {node_id} has latest ledger state {status}",
            )
    for node_id in sorted(BLOCKED_REFINEMENT_PARENTS):
        status = _latest_ledger_status(records, node_id, include_descendants=False)
        if status != "BLOCKED":
            _fail(
                "atg/ledger.jsonl",
                f"blocked refinement parent {node_id} has latest ledger state {status}",
            )
    for node_id in sorted(ACCEPTED_REFINEMENT_PARENTS):
        status = _latest_ledger_status(records, node_id, include_descendants=False)
        if status != "ACCEPTED":
            _fail(
                "atg/ledger.jsonl",
                f"accepted refinement parent {node_id} has latest ledger state {status}",
            )
    return (
        len(CURRENT_ACCEPTED_WHOLE)
        + len(CURRENT_ACCEPTED_REFINEMENTS)
        + len(CURRENT_ACCEPTED_CHILDREN)
        + len(SUPERSEDED_HISTORICAL_CHILDREN)
        + len(BLOCKED_PARENT_CAPABILITIES)
        + len(BLOCKED_REFINEMENT_PARENTS)
        + len(ACCEPTED_REFINEMENT_PARENTS)
    )


def _validate_current_output_hashes(records: list[dict[str, Any]]) -> int:
    current_record = next(
        (
            record
            for record in reversed(records)
            if record["node_id"] == CURRENT_OUTPUT_AUTHORITY_NODE
            or record["node_id"].startswith(f"{CURRENT_OUTPUT_AUTHORITY_NODE}.V")
        ),
        None,
    )
    if current_record is None or current_record["status"] != "ACCEPTED":
        _fail(
            "atg/ledger.jsonl",
            f"current output authority {CURRENT_OUTPUT_AUTHORITY_NODE} is not ACCEPTED",
        )
    outputs = current_record["output_artifacts"]
    if not isinstance(outputs, dict):
        _fail("atg/ledger.jsonl", "current output_artifacts must be an object")
    expected = {
        "REQUIREMENTS.md": REQUIREMENTS_PATH,
        "ATG_PLAN.md": ATG_PLAN_PATH,
        "ATG_LOOP.md": ATG_LOOP_PATH,
        "scripts/validate_atg_documents.py": PROJECT_ROOT
        / "scripts/validate_atg_documents.py",
        "tests/test_atg_documents.py": PROJECT_ROOT / "tests/test_atg_documents.py",
        "metrics/metric-registry.json": PROJECT_ROOT / "metrics/metric-registry.json",
        "scripts/validate_metric_registry.py": PROJECT_ROOT / "scripts/validate_metric_registry.py",
        "tests/test_metric_registry.py": PROJECT_ROOT / "tests/test_metric_registry.py",
        "resources/resource-registry.json": PROJECT_ROOT
        / "resources/resource-registry.json",
        "resources/manifests/lfw-local-pairs-v1.jsonl": PROJECT_ROOT
        / "resources/manifests/lfw-local-pairs-v1.jsonl",
        "scripts/validate_lfw_protocol.py": PROJECT_ROOT
        / "scripts/validate_lfw_protocol.py",
        "tests/test_lfw_protocol.py": PROJECT_ROOT / "tests/test_lfw_protocol.py",
        "tests/test_resource_registry.py": PROJECT_ROOT
        / "tests/test_resource_registry.py",
        "scripts/validate_resource_registry.py": PROJECT_ROOT
        / "scripts/validate_resource_registry.py",
        "resources/manifests/lfw-full-v1-shared-crop-lineage-reconciliation.json": PROJECT_ROOT
        / "resources/manifests/lfw-full-v1-shared-crop-lineage-reconciliation.json",
        "scripts/validate_shared_crop_lineage.py": PROJECT_ROOT
        / "scripts/validate_shared_crop_lineage.py",
        "tests/test_shared_crop_lineage.py": PROJECT_ROOT
        / "tests/test_shared_crop_lineage.py",
        "resources/authorizations/qualsight-lfw-protocol-evidence-review-2026-08-17.json": PROJECT_ROOT
        / "resources/authorizations/qualsight-lfw-protocol-evidence-review-2026-08-17.json",
        "resources/authorizations/qualsight-lfw-original-protocol-evidence-review-2026-08-17.json": PROJECT_ROOT
        / "resources/authorizations/qualsight-lfw-original-protocol-evidence-review-2026-08-17.json",
        "resources/upstream/lfw/lfw-2007.pdf": PROJECT_ROOT
        / "resources/upstream/lfw/lfw-2007.pdf",
        "resources/upstream/lfw/lfw-2007.headers": PROJECT_ROOT
        / "resources/upstream/lfw/lfw-2007.headers",
        "resources/upstream/lfw/Huang_eccv2008-lfw.pdf": PROJECT_ROOT
        / "resources/upstream/lfw/Huang_eccv2008-lfw.pdf",
        "resources/upstream/lfw/Huang_eccv2008-lfw.headers": PROJECT_ROOT
        / "resources/upstream/lfw/Huang_eccv2008-lfw.headers",
        "resources/upstream/lfw/UM-CS-2014-003.pdf": PROJECT_ROOT
        / "resources/upstream/lfw/UM-CS-2014-003.pdf",
        "resources/upstream/lfw/UM-CS-2014-003.headers": PROJECT_ROOT
        / "resources/upstream/lfw/UM-CS-2014-003.headers",
        "resources/manifests/lfw-official-protocol-authority-v1.json": PROJECT_ROOT
        / "resources/manifests/lfw-official-protocol-authority-v1.json",
        "scripts/validate_lfw_protocol_authority.py": PROJECT_ROOT
        / "scripts/validate_lfw_protocol_authority.py",
        "tests/test_lfw_protocol_authority.py": PROJECT_ROOT
        / "tests/test_lfw_protocol_authority.py",
        "resources/authorizations/qualsight-insightface-buffalo-l-evidence-acquisition-2026-08-17.json": PROJECT_ROOT
        / "resources/authorizations/qualsight-insightface-buffalo-l-evidence-acquisition-2026-08-17.json",
        "resources/upstream/insightface/release-v0.7.json": PROJECT_ROOT
        / "resources/upstream/insightface/release-v0.7.json",
        "resources/upstream/insightface/release-v0.7.headers": PROJECT_ROOT
        / "resources/upstream/insightface/release-v0.7.headers",
        "resources/upstream/insightface/buffalo_l-v0.7-final.headers": PROJECT_ROOT
        / "resources/upstream/insightface/buffalo_l-v0.7-final.headers",
        "resources/manifests/insightface-buffalo-l-release-provenance-v1.json": PROJECT_ROOT
        / "resources/manifests/insightface-buffalo-l-release-provenance-v1.json",
        "scripts/validate_scrfd_release_provenance.py": PROJECT_ROOT
        / "scripts/validate_scrfd_release_provenance.py",
        "tests/test_scrfd_release_provenance.py": PROJECT_ROOT
        / "tests/test_scrfd_release_provenance.py",
        "resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json": PROJECT_ROOT
        / "resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json",
        "scripts/ofiq_v112_build.sh": PROJECT_ROOT / "scripts/ofiq_v112_build.sh",
        "scripts/repin_registry_identity.py": PROJECT_ROOT / "scripts/repin_registry_identity.py",
        "scripts/observe_console.py": PROJECT_ROOT / "scripts/observe_console.py",
        "scripts/append_ledger_record.py": PROJECT_ROOT / "scripts/append_ledger_record.py",
        "atg/evidence/2026-08-17-console-observation-01.json": PROJECT_ROOT
        / "atg/evidence/2026-08-17-console-observation-01.json",
        "backend/pyproject.toml": PROJECT_ROOT / "backend/pyproject.toml",
        "uv.lock": PROJECT_ROOT / "uv.lock",
        "package.json": PROJECT_ROOT / "package.json",
        "frontend/package.json": PROJECT_ROOT / "frontend/package.json",
        "pnpm-lock.yaml": PROJECT_ROOT / "pnpm-lock.yaml",
        "backend/tests/test_execution_cli_real_ofiq_v112.py": PROJECT_ROOT
        / "backend/tests/test_execution_cli_real_ofiq_v112.py",
        "backend/tests/engines/test_ofiq_engine_real.py": PROJECT_ROOT
        / "backend/tests/engines/test_ofiq_engine_real.py",
        "backend/tests/api/test_quality_api_real.py": PROJECT_ROOT
        / "backend/tests/api/test_quality_api_real.py",
        "backend/tests/test_reproduction_real.py": PROJECT_ROOT
        / "backend/tests/test_reproduction_real.py",
        "backend/tests/fixtures/ofiq_r01_completed_run.json": PROJECT_ROOT
        / "backend/tests/fixtures/ofiq_r01_completed_run.json",
        "backend/tests/api/test_project_lifecycle_real.py": PROJECT_ROOT
        / "backend/tests/api/test_project_lifecycle_real.py",
        "backend/tests/api/test_dataset_import_real.py": PROJECT_ROOT
        / "backend/tests/api/test_dataset_import_real.py",
        "backend/tests/api/test_quality_batch_real.py": PROJECT_ROOT
        / "backend/tests/api/test_quality_batch_real.py",
        "resources/manifests/lfw-subset-fixture-v1.json": PROJECT_ROOT
        / "resources/manifests/lfw-subset-fixture-v1.json",
        "atg/evidence/2026-08-17-console-observation-03.json": PROJECT_ROOT
        / "atg/evidence/2026-08-17-console-observation-03.json",
        "atg/evidence/2026-08-17-console-observation-04.json": PROJECT_ROOT
        / "atg/evidence/2026-08-17-console-observation-04.json",
        "atg/evidence/2026-08-17-console-observation-05.json": PROJECT_ROOT
        / "atg/evidence/2026-08-17-console-observation-05.json",
        "resources/manifests/matcher-upstream-sources-v1.json": PROJECT_ROOT
        / "resources/manifests/matcher-upstream-sources-v1.json",
        "backend/tests/api/test_protocol_real.py": PROJECT_ROOT / "backend/tests/api/test_protocol_real.py",
        "backend/tests/test_matcher_runtime_real.py": PROJECT_ROOT / "backend/tests/test_matcher_runtime_real.py",
        "backend/tests/test_matcher_matrix_real.py": PROJECT_ROOT / "backend/tests/test_matcher_matrix_real.py",
        "backend/tests/api/test_matcher_api_real.py": PROJECT_ROOT / "backend/tests/api/test_matcher_api_real.py",
        "atg/evidence/2026-08-17-console-observation-06.json": PROJECT_ROOT / "atg/evidence/2026-08-17-console-observation-06.json",
        "scripts/verify_utility_analysis.py": PROJECT_ROOT / "scripts/verify_utility_analysis.py",
        "scripts/verify_cross_matcher.py": PROJECT_ROOT / "scripts/verify_cross_matcher.py",
        "atg/evidence/2026-08-17-console-observation-07.json": PROJECT_ROOT / "atg/evidence/2026-08-17-console-observation-07.json",
        "atg/evidence/2026-08-17-console-observation-08.json": PROJECT_ROOT / "atg/evidence/2026-08-17-console-observation-08.json",
        "atg/evidence/2026-08-17-offline-gate-01.json": PROJECT_ROOT / "atg/evidence/2026-08-17-offline-gate-01.json",
        "atg/evidence/2026-08-17-docs-workflow-gate-01.json": PROJECT_ROOT / "atg/evidence/2026-08-17-docs-workflow-gate-01.json",
        "docs/USER_GUIDE.md": PROJECT_ROOT / "docs/USER_GUIDE.md",
        "docs/INSTALL.md": PROJECT_ROOT / "docs/INSTALL.md",
        "docs/SCIENTIFIC_METHODS.md": PROJECT_ROOT / "docs/SCIENTIFIC_METHODS.md",
        "docs/THREAT_MODEL.md": PROJECT_ROOT / "docs/THREAT_MODEL.md",
        "scripts/install.sh": PROJECT_ROOT / "scripts/install.sh",
        "scripts/offline_gate.py": PROJECT_ROOT / "scripts/offline_gate.py",
        "scripts/docs_workflow_gate.py": PROJECT_ROOT / "scripts/docs_workflow_gate.py",
        "scripts/audit_local_data_controls.py": PROJECT_ROOT / "scripts/audit_local_data_controls.py",
        "resources/authorizations/qualsight-owner-phase2-amendment-2026-08-17.json": PROJECT_ROOT / "resources/authorizations/qualsight-owner-phase2-amendment-2026-08-17.json",
    }
    for label, path in expected.items():
        actual = _sha256(path)
        if outputs.get(label) != actual:
            _fail(
                "atg/ledger.jsonl",
                f"{CURRENT_OUTPUT_AUTHORITY_NODE} does not bind current {label}: {actual}",
            )
    return len(expected)


def validate_documents() -> dict[str, int]:
    prd_text = PRD_PATH.read_text(encoding="utf-8")
    requirements_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    plan_text = ATG_PLAN_PATH.read_text(encoding="utf-8")
    loop_text = ATG_LOOP_PATH.read_text(encoding="utf-8")
    if "**Registry version:** v0.2" not in requirements_text:
        _fail("REQUIREMENTS.md", "registry version must be v0.2")
    if "**Graph version:** v1.6" not in plan_text:
        _fail("ATG_PLAN.md", "graph version must be v1.6")
    if "**Control contract:** v1.6" not in loop_text:
        _fail("ATG_LOOP.md", "control contract must be v1.6")
    for path, text in (
        (REQUIREMENTS_PATH, requirements_text),
        (ATG_PLAN_PATH, plan_text),
        (ATG_LOOP_PATH, loop_text),
    ):
        marker = FORBIDDEN_MARKER_RE.search(text)
        if marker is not None:
            _fail(path.name, f"unresolved marker {marker.group(0)!r}")
    _validate_frozen_sources(requirements_text)
    aliases, edges = _capability_graph(plan_text)
    capability_states = _validate_capability_table(plan_text, aliases, edges)
    known_nodes = {
        parts[0]
        for line in plan_text.splitlines()
        if line.startswith("| QS-")
        for parts in [_table_parts(line)]
        if parts and CAPABILITY_RE.fullmatch(parts[0])
    }
    requirements, sections, decisions = _validate_requirements(
        requirements_text,
        prd_text,
        known_nodes,
    )
    _validate_controller_snapshot(plan_text, loop_text)
    matcher_transforms, preprocessing_conditions, matcher_condition_leaves = (
        _validate_matcher_matrix(plan_text)
    )
    ledger_records, ledger, historical_prefix_records = _validate_ledger()
    ledger_state_claims = _validate_ledger_state_claims(ledger)
    current_output_hashes = _validate_current_output_hashes(ledger)
    return {
        "requirements": requirements,
        "sections": sections,
        "decisions": decisions,
        "nodes": len(aliases),
        "edges": len(edges),
        "ledger_records": ledger_records,
        "accepted_whole_capabilities": len(CURRENT_ACCEPTED_WHOLE),
        "current_accepted_refinements": len(CURRENT_ACCEPTED_REFINEMENTS),
        "current_accepted_children": len(CURRENT_ACCEPTED_CHILDREN),
        "superseded_historical_children": len(SUPERSEDED_HISTORICAL_CHILDREN),
        "blocked_parent_capabilities": len(BLOCKED_PARENT_CAPABILITIES),
        "blocked_refinement_parents": len(BLOCKED_REFINEMENT_PARENTS),
        "ledger_state_claims": ledger_state_claims,
        "historical_prefix_records": historical_prefix_records,
        "current_output_hashes": current_output_hashes,
        "matcher_transforms": matcher_transforms,
        "preprocessing_conditions": preprocessing_conditions,
        "matcher_condition_leaves": matcher_condition_leaves,
    }


def main() -> int:
    try:
        summary = validate_documents()
    except (OSError, UnicodeDecodeError, ATGDocumentValidationError) as error:
        print(f"ATG_DOCUMENTS_INVALID: {error}", file=sys.stderr)
        return 1
    print(
        "ATG_DOCUMENTS_VALID "
        + " ".join(f"{key}={value}" for key, value in summary.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
