"""Real external score importer (PRD §§12, 51.8, 54 Tier 1; QS-C21).

Imports a *registered* score artifact that some system other than QualSight
produced over a frozen protocol. Every row must map to a recorded protocol pair
by pair id (and, when the artifact carries image paths, those paths must match
the recorded samples); duplicates, unknown pairs, mismatched labels, and paths
that disagree with the protocol are refused — no row is invented. Protocol
pairs the artifact does not cover are kept as explicit ``NOT_EXECUTED``
failures so the Run still holds every protocol pair. The resulting Run carries
the permanent provenance marker ``EXTERNALLY_COMPUTED`` in its configuration,
its matcher binding, and its leaf identity; the verification metrics use the
same registered definitions as QualSight-executed Runs.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.comparison import ComparisonLabel, ProcessingFailureKind, ProvenanceMarker, ScoreDirection
from ..domain.entities import Artifact, ArtifactKind, DataClassification, StorageBackend
from ..domain.primitives import ArtifactIdentity as DomainArtifactIdentity, CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import _not_applicable, content_id
from ..execution.cli_runtime import USABLE_RESOURCE_STATES
from ..metrics.verification import ComparisonInputs, compute_verification_metrics
from ..provenance import application_version, environment_identity, hardware_identity, repository_identity, sha256_bytes, sha256_file

EXTERNAL_LEAF_ID = "QS-C21.EXTERNAL"
EXTERNAL_CONDITION_ID = "EXTERNALLY_COMPUTED"
IMPORTER_VERSION = "1.0.0"
REQUIRED_COLUMNS = ("pair_id", "score")


class ExternalScoreImportError(RuntimeError):
    """Raised when the artifact cannot be mapped to the protocol without inventing or dropping rows silently."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True, slots=True)
class ExternalImportResult:
    leaf_id: str
    matcher_id: str
    condition_id: str
    run: CompletedRun
    metrics: dict[str, Any]
    comparisons_parquet: Path
    embeddings_path: Path | None
    comparison_count: int
    scored_count: int
    failed_count: int
    mapping: dict[str, Any]


class ExternalScoreImporter:
    """Import one registered external score artifact over one frozen protocol into an immutable Run."""

    def __init__(self, *, store: Any, registry: Any, repository_root: Path, project: Any, protocol: Any, resource_id: str, job_root: Path, actor: str, purpose_scope: str = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1") -> None:
        self.store = store
        self.registry = registry
        self.repository_root = repository_root
        self.project = project
        self.protocol = protocol
        self.resource_id = resource_id
        self.actor = actor
        self.purpose_scope = purpose_scope
        raw = registry.raw_resource(resource_id)
        if raw["state"] not in USABLE_RESOURCE_STATES:
            raise ExternalScoreImportError(f"{resource_id} is in registry state {raw['state']}; not usable")
        if purpose_scope not in raw.get("approved_scopes", []):
            raise ExternalScoreImportError(f"{resource_id} does not approve scope {purpose_scope}")
        if raw.get("kind") != "EXTERNAL_SCORE_ARTIFACT":
            raise ExternalScoreImportError(f"{resource_id} is not registered as an external score artifact")
        self.raw = raw
        resource = registry.resource(resource_id)
        data = [a for a in resource.artifacts if a.role == "DATA"]
        docs = [a for a in resource.artifacts if a.role == "DOCUMENTATION"]
        if len(data) != 1:
            raise ExternalScoreImportError("the artifact must register exactly one DATA file (the score table)")
        self.score_identity = data[0].identity
        self.sidecar_identity = docs[0].identity if docs else None
        for identity in (self.score_identity, self.sidecar_identity):
            if identity is not None and sha256_file(Path(identity.path)) != identity.sha256:
                raise ExternalScoreImportError(f"{identity.path} drifted from its registered hash")
        self.dataset = store.dataset(protocol.dataset_id)
        if self.dataset is None:
            raise ExternalScoreImportError("protocol dataset is not recorded")
        self.matrix_id = content_id("external-import", project.project_id, protocol.protocol_id, resource_id, self.score_identity.sha256)
        self.job_dir = job_root / self.matrix_id.replace(":", "_")
        if self.job_dir.exists():
            raise ExternalScoreImportError("this artifact is already imported for this protocol and project")

    # -- mapping ------------------------------------------------------------------

    def _mapped_rows(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pairs = self.store.protocol_pairs(self.protocol.protocol_id, limit=1_000_000)
        by_id = {pair.pair_id: pair for pair in pairs}
        samples: dict[str, Any] = {}
        for pair in pairs:
            for sample_id in (pair.left_sample_id, pair.right_sample_id):
                if sample_id not in samples:
                    samples[sample_id] = self.store.sample(sample_id)
        with Path(self.score_identity.path).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            missing = [c for c in REQUIRED_COLUMNS if c not in columns]
            if missing:
                raise ExternalScoreImportError(f"score table lacks required columns {missing}")
            rows = list(reader)
        seen: set[str] = set()
        refusals: list[str] = []
        by_pair: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows, start=2):
            pair_id = (row.get("pair_id") or "").strip()
            pair = by_id.get(pair_id)
            if pair is None:
                refusals.append(f"line {index}: pair {pair_id!r} is not part of the protocol")
                continue
            if pair_id in seen:
                refusals.append(f"line {index}: duplicate pair {pair_id}")
                continue
            seen.add(pair_id)
            for side in ("left", "right"):
                given = (row.get(f"{side}_path") or "").strip()
                if given:
                    recorded = samples[getattr(pair, f"{side}_sample_id")].source_path
                    if not recorded.endswith("/" + given.lstrip("/")) and recorded != given:
                        refusals.append(f"line {index}: {side} path {given!r} does not match the recorded sample {recorded}")
            label = (row.get("label") or "").strip()
            if label and label != pair.label.value:
                refusals.append(f"line {index}: label {label!r} disagrees with the protocol ({pair.label.value})")
            by_pair[pair_id] = row
        if refusals:
            raise ExternalScoreImportError("artifact refused: " + "; ".join(refusals[:10]) + (f" … ({len(refusals)} problems)" if len(refusals) > 10 else ""))
        mapped: list[dict[str, Any]] = []
        for pair in sorted(pairs, key=lambda item: (item.fold, item.source_order)):
            row = by_pair.get(pair.pair_id)
            score: float | None = None
            failure: str | None = None
            side: str | None = None
            if row is None:
                failure = ProcessingFailureKind.NOT_EXECUTED.value
                side = "left+right"
            else:
                text = (row.get("score") or "").strip()
                if text == "":
                    failure = ProcessingFailureKind.FAILURE_TO_TEMPLATE.value
                    side = (row.get("failure") or "").strip() or "unspecified"
                else:
                    try:
                        score = float(text)
                    except ValueError as error:
                        raise ExternalScoreImportError(f"pair {pair.pair_id}: score {text!r} is not numeric") from error
                    if not np.isfinite(score):
                        failure = ProcessingFailureKind.FAILURE_TO_COMPARE.value
                        side = "score-not-finite"
                        score = None
            mapped.append({"pair_id": pair.pair_id, "fold": pair.fold, "source_order": pair.source_order, "label": pair.label.value, "left_sample_id": pair.left_sample_id, "right_sample_id": pair.right_sample_id,
                           "left_subject_id": pair.left_subject_id, "right_subject_id": pair.right_subject_id,
                           "score": score, "failure": failure, "failure_side": side, "left_face_count": None, "right_face_count": None})
        mapping = {"artifact_rows": len(rows), "protocol_pairs": len(pairs), "mapped_pairs": len(by_pair), "protocol_pairs_without_row": len(pairs) - len(by_pair), "columns": columns, "refusals": 0, "rule": "rows map to protocol pairs by pair_id; paths and labels, when present, must agree with the recorded protocol; no row is invented; uncovered pairs are NOT_EXECUTED failures"}
        return mapped, mapping

    # -- run --------------------------------------------------------------------

    def run(self) -> ExternalImportResult:
        started = _now()
        clock = time.perf_counter()
        rows, mapping = self._mapped_rows()
        self.job_dir.mkdir(parents=True, exist_ok=False)
        leaf_dir = self.job_dir / EXTERNAL_LEAF_ID.replace(".", "_")
        leaf_dir.mkdir()
        table = pa.Table.from_pylist(rows, schema=pa.schema([
            pa.field("pair_id", pa.string()), pa.field("fold", pa.int32()), pa.field("source_order", pa.int32()), pa.field("label", pa.string()),
            pa.field("left_sample_id", pa.string()), pa.field("right_sample_id", pa.string()), pa.field("left_subject_id", pa.string()), pa.field("right_subject_id", pa.string()),
            pa.field("score", pa.float64()), pa.field("failure", pa.string()), pa.field("failure_side", pa.string()), pa.field("left_face_count", pa.int32()), pa.field("right_face_count", pa.int32()),
        ]))
        comparisons_path = leaf_dir / "comparisons.parquet"
        pq.write_table(table, comparisons_path, compression="zstd")
        metrics = compute_verification_metrics(ComparisonInputs(
            scores=np.array([r["score"] if r["score"] is not None else np.nan for r in rows], dtype=np.float64),
            mated=np.array([r["label"] == ComparisonLabel.MATED.value for r in rows]),
            failed=np.array([r["failure"] is not None for r in rows]),
            folds=np.array([r["fold"] for r in rows], dtype=np.int64),
        ))
        metrics_path = leaf_dir / "verification_metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        seconds = time.perf_counter() - clock
        scored = sum(1 for r in rows if r["score"] is not None)
        sidecar = json.loads(Path(self.sidecar_identity.path).read_text(encoding="utf-8")) if self.sidecar_identity else {}
        producer = sidecar.get("producer", {})
        configuration = CanonicalJson.from_value({
            "leaf_id": EXTERNAL_LEAF_ID, "matrix_id": self.matrix_id, "provenance_marker": ProvenanceMarker.EXTERNALLY_COMPUTED.value, "importer_version": IMPORTER_VERSION,
            "protocol_id": self.protocol.protocol_id, "protocol_pairs_sha256": self.protocol.pairs_sha256, "pair_manifest_sha256": self.protocol.pair_manifest.sha256,
            "artifact": {"resource_id": self.resource_id, "path": self.score_identity.path, "sha256": self.score_identity.sha256, "size_bytes": self.score_identity.size_bytes, "sidecar_sha256": self.sidecar_identity.sha256 if self.sidecar_identity else None},
            "producer": producer, "score": {"definition": sidecar.get("score_definition", "as declared by the external producer"), "direction": sidecar.get("score_direction", ScoreDirection.HIGHER_IS_MORE_SIMILAR.value)},
            "mapping": mapping, "purpose_scope": self.purpose_scope,
            "failure_policy": "every protocol pair retained; an artifact row without a score is FAILURE_TO_TEMPLATE with the producer's failure text as side; a protocol pair without a row is NOT_EXECUTED",
        })
        if configuration.text and sidecar.get("score_direction", "HIGHER_IS_MORE_SIMILAR") != ScoreDirection.HIGHER_IS_MORE_SIMILAR.value:
            raise ExternalScoreImportError("only HIGHER_IS_MORE_SIMILAR score artifacts are supported by the registered verification definitions")
        experiment_id = content_id("experiment", self.project.project_id, self.dataset.dataset_id, self.protocol.protocol_id, self.resource_id, EXTERNAL_CONDITION_ID)
        run_id = content_id("run", experiment_id, started.isoformat(), sha256_file(comparisons_path), self.score_identity.sha256)
        policy = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
        generalized = metrics["by_policy"]["FAILURES_AS_REJECT"]
        observations = [
            MetricObservation(metric_id="comparisons.total", value=len(rows), unit="count"), MetricObservation(metric_id="comparisons.scored", value=scored, unit="count"),
            MetricObservation(metric_id="comparisons.failed", value=len(rows) - scored, unit="count"), MetricObservation(metric_id="import.artifact_rows", value=mapping["artifact_rows"], unit="count"),
            MetricObservation(metric_id="biometric.verification.eer@1.0.0[EXCLUDE_FAILED_TRIALS]", value=float(policy["eer"]["eer"]), unit="rate"),
            MetricObservation(metric_id="biometric.verification.eer@1.0.0[FAILURES_AS_REJECT]", value=float(generalized["eer"]["eer"]), unit="rate"),
            MetricObservation(metric_id="timing.import_seconds", value=round(seconds, 3), unit="observation"),
        ]
        for name, block in (("EXCLUDE_FAILED_TRIALS", policy), ("FAILURES_AS_REJECT", generalized)):
            if block["view2_tenfold"]["mean_accuracy"] is not None:
                observations.append(MetricObservation(metric_id=f"view2.tenfold.mean_accuracy[{name}]", value=float(block["view2_tenfold"]["mean_accuracy"]), unit="rate"))
        for point in policy["operating_points"]:
            if point.get("threshold") is not None:
                observations.append(MetricObservation(metric_id=f"biometric.verification.fnmr@1.0.0[EXCLUDE_FAILED_TRIALS]@fmr<={point['target_fmr']}", value=float(point["fnmr"]), unit="rate"))
        artifacts = []
        for path, kind, media, backend in ((comparisons_path, ArtifactKind.TABLE, "application/vnd.apache.parquet", StorageBackend.PARQUET), (metrics_path, ArtifactKind.METRICS, "application/json", StorageBackend.FILESYSTEM)):
            digest = sha256_file(path)
            artifacts.append(Artifact(artifact_id=content_id("artifact", run_id, digest, path.name), run_id=run_id, kind=kind, storage_backend=backend, uri=str(path), sha256=digest, size_bytes=path.stat().st_size, media_type=media, classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED, created_at=_now()))
        score_domain = DomainArtifactIdentity(path=self.score_identity.path, sha256=self.score_identity.sha256, size_bytes=self.score_identity.size_bytes, mtime_ns=self.score_identity.mtime_ns)
        model_hashes = [score_domain]
        if self.sidecar_identity is not None:
            model_hashes.append(DomainArtifactIdentity(path=self.sidecar_identity.path, sha256=self.sidecar_identity.sha256, size_bytes=self.sidecar_identity.size_bytes, mtime_ns=self.sidecar_identity.mtime_ns))
        producer_text = json.dumps(producer, sort_keys=True)
        run = CompletedRun(
            run_id=run_id, project_id=self.project.project_id, experiment_id=experiment_id, user_id=self.actor,
            dataset_manifest=self.dataset.manifest, dataset_hash=self.dataset.manifest.sha256,
            subject_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            pair_search_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.protocol.protocol_id, version="LFW_VIEW2", configuration_sha256=self.protocol.pairs_sha256),
            quality_engine=None, quality_engine_configuration=None, model_hashes=tuple(model_hashes),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.resource_id, version=self.score_identity.sha256[:16], configuration_sha256=sha256_bytes(producer_text.encode("utf-8")), artifact_hashes=tuple(identity.sha256 for identity in model_hashes)),
            matcher_preprocessing=ComponentBinding(state=ComponentState.CONFIGURED, component_id=EXTERNAL_CONDITION_ID, version=IMPORTER_VERSION, configuration_sha256=sha256_bytes(producer_text.encode("utf-8"))),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="registry-defined-operating-points", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"targets": [0.1, 0.01, 0.001], "eer": "linear interpolation"}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.verification", version="1.0.0", configuration_sha256=sha256_bytes(b"clopper-pearson binomial CI; view2 tenfold accuracy")),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="CLOPPER_PEARSON_EXACT_BINOMIAL_95", version="1.0.0"),
            randomness=_not_applicable("Importing recorded scores and computing rate metrics contains no random-seed setting."), random_seeds=(),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"), repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", {}), hardware=hardware_identity("import and metrics on CPU; scores were computed externally"),
            start_time=started, end_time=_now(), runtime_seconds=round(seconds, 3), initialization_ms=0, assessment_ms=int(seconds * 1000),
            configuration=configuration, metrics=tuple(observations), artifacts=tuple(artifacts),
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=tuple(w for w in (f"{mapping['protocol_pairs_without_row']} protocol pairs have no artifact row and are NOT_EXECUTED" if mapping["protocol_pairs_without_row"] else None, f"{len(rows) - scored} pairs carry no score (external failure or not executed)" if len(rows) - scored else None) if w),
            provenance_limits=tuple(self.raw.get("use_restrictions", ())) + ("EXTERNALLY COMPUTED: scores were produced outside QualSight by the declared producer; QualSight imported and evaluated them without re-scoring.",) + tuple(self.protocol.limitations),
            exit_code=0, status=RunStatus.COMPLETED, scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        result = ExternalImportResult(EXTERNAL_LEAF_ID, self.resource_id, EXTERNAL_CONDITION_ID, run, metrics, comparisons_path, None, len(rows), scored, len(rows) - scored, mapping)
        self.store.record_matcher_leaf(self, _LeafView(result))
        return result


class _LeafView:
    """Adapter giving the store's record_matcher_leaf the attributes it reads from a matrix leaf."""

    def __init__(self, result: ExternalImportResult) -> None:
        self.leaf_id = result.leaf_id
        self.matcher_id = _Named(result.matcher_id)
        self.condition_id = _Named(result.condition_id)
        self.run = result.run
        self.metrics = result.metrics
        self.comparisons_parquet = result.comparisons_parquet
        self.comparison_count = result.comparison_count
        self.scored_count = result.scored_count
        self.failed_count = result.failed_count


class _Named:
    def __init__(self, value: str) -> None:
        self.value = value
