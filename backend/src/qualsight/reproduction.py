"""Reproduce a recorded scientific Run from its exact registered inputs.

A reproduction re-executes the same registered engine on the same registered
input bytes (both re-verified by SHA-256 against the recorded Run and the live
registry) and compares every non-timing metric. Timing fields are observations
and are excluded from the equality gate. The outcome is a
:class:`~qualsight.domain.run.RunAttestation` of kind REPRODUCED whose
``reproduction_state`` is EXACT, DIFFERENT, MISSING_DATA or MISSING_DEPENDENCY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
from pathlib import Path

from .api.store import RunStore
from .domain.run import (
    CompletedRun,
    ReproductionState,
    RunAttestation,
    RunAttestationKind,
)
from .engines.ofiq.adapter import OFIQEngineError, OFIQQualityEngine, content_id
from .execution.contracts import ContractValidationError, ExecutionOutcomeError
from .provenance import artifact_identity


@dataclass(frozen=True, slots=True)
class ReproductionReport:
    original_run_id: str
    reproduction_run_id: str | None
    state: ReproductionState
    compared_metrics: int
    differing_metrics: tuple[str, ...]
    missing: tuple[str, ...]
    attestation: RunAttestation
    evidence: dict[str, object]


def _verify_recorded_identities(run: CompletedRun) -> tuple[str, ...]:
    """Every byte the recorded Run names must still exist with the same digest."""

    missing: list[str] = []
    identities = [*run.model_hashes]
    if run.quality_engine is not None and run.quality_engine_configuration is not None:
        identities = [run.quality_engine_configuration, *run.quality_engine.artifacts, *identities]
    for identity in identities:
        try:
            current = artifact_identity(Path(identity.path))
        except (OSError, ValueError):
            missing.append(f"{identity.path}: absent")
            continue
        if current.sha256 != identity.sha256 or current.size_bytes != identity.size_bytes:
            missing.append(f"{identity.path}: sha256 {current.sha256} != recorded {identity.sha256}")
    for artifact in run.artifacts:
        try:
            current = artifact_identity(Path(artifact.uri))
        except (OSError, ValueError):
            missing.append(f"{artifact.uri}: absent")
            continue
        if current.sha256 != artifact.sha256:
            missing.append(f"{artifact.uri}: sha256 {current.sha256} != recorded {artifact.sha256}")
    return tuple(missing)


def _values_equal(left: int | float, right: int | float) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=0.0)
    return left == right


def reproduce_run(
    *,
    store: RunStore,
    engine: OFIQQualityEngine,
    run_id: str,
    actor_id: str | None = None,
    record_reproduction: bool = True,
) -> ReproductionReport:
    found = store.run(run_id)
    if found is None:
        raise KeyError(run_id)
    original, original_evidence = found
    if original.quality_engine is None:
        raise KeyError(f"{run_id} is not a quality-engine Run; matcher Runs are reproduced by the matcher matrix service")
    actor = actor_id or f"local-user:{getpass.getuser()}"
    created_at = datetime.now(timezone.utc).astimezone()
    purpose_scope = json.loads(original.configuration.text)["approved_scope"]
    input_path = original.artifacts[0].uri

    def attest(state: ReproductionState, evidence: dict[str, object], reproduction_run_id: str | None,
               compared: int, differing: tuple[str, ...], missing: tuple[str, ...]) -> ReproductionReport:
        evidence_text = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        attestation = RunAttestation(
            attestation_id=content_id("attestation", run_id, state.value, created_at.isoformat()),
            run_id=run_id,
            kind=RunAttestationKind.REPRODUCED,
            created_at=created_at,
            actor_id=actor,
            evidence_sha256=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
            reproduction_state=state,
        )
        return ReproductionReport(
            original_run_id=run_id,
            reproduction_run_id=reproduction_run_id,
            state=state,
            compared_metrics=compared,
            differing_metrics=differing,
            missing=missing,
            attestation=attestation,
            evidence=evidence,
        )

    missing = _verify_recorded_identities(original)
    base_evidence: dict[str, object] = {
        "original_run_id": run_id,
        "original_evidence_sha256": original_evidence,
        "engine_resource_id": original.quality_engine.resource_id,
        "engine_version": original.quality_engine.version,
        "input_path": input_path,
        "purpose_scope": purpose_scope,
        "registry_sha256": engine.registry.file_identity.sha256,
    }
    if missing:
        return attest(ReproductionState.MISSING_DATA, {**base_evidence, "missing": list(missing)}, None, 0, (), missing)
    if engine.runtime.resource_id != original.quality_engine.resource_id or engine.runtime.version != original.quality_engine.version:
        detail = (
            f"bound engine {engine.runtime.resource_id} v{engine.runtime.version} differs from recorded "
            f"{original.quality_engine.resource_id} v{original.quality_engine.version}"
        )
        return attest(ReproductionState.MISSING_DEPENDENCY, {**base_evidence, "missing": [detail]}, None, 0, (), (detail,))
    try:
        result = engine.assess(input_path, purpose_scope)
    except (ContractValidationError, ExecutionOutcomeError, OFIQEngineError) as error:
        detail = f"re-execution refused or failed: {error}"
        return attest(ReproductionState.BLOCKED, {**base_evidence, "missing": [detail]}, None, 0, (), (detail,))
    if not result.succeeded:
        detail = f"re-execution produced an explicit engine failure: {result.row.failure_reason}"
        return attest(ReproductionState.DIFFERENT, {**base_evidence, "missing": [detail]}, None, 0, (), (detail,))
    bundle = engine.build_run(result, purpose_scope=purpose_scope, actor_id=actor)
    reproduction_run_id: str | None = None
    if record_reproduction:
        store.record_bundle(bundle)
        reproduction_run_id = bundle.run.run_id
    original_metrics = {metric.metric_id: metric.value for metric in original.metrics}
    new_metrics = {metric.metric_id: metric.value for metric in bundle.run.metrics}
    differing = tuple(
        sorted(
            metric_id
            for metric_id in set(original_metrics) | set(new_metrics)
            if metric_id not in original_metrics
            or metric_id not in new_metrics
            or not _values_equal(original_metrics[metric_id], new_metrics[metric_id])
        )
    )
    state = ReproductionState.EXACT if not differing else ReproductionState.DIFFERENT
    evidence = {
        **base_evidence,
        "reproduction_run_id": bundle.run.run_id,
        "compared_metrics": len(set(original_metrics) | set(new_metrics)),
        "differing_metrics": list(differing),
        "original_model_hashes": [item.sha256 for item in original.model_hashes],
        "reproduction_model_hashes": [item.sha256 for item in bundle.run.model_hashes],
        "timing_excluded": ["initialization_ms", "assessment_ms", "runtime_seconds", "start_time", "end_time"],
    }
    return attest(state, evidence, reproduction_run_id, len(set(original_metrics) | set(new_metrics)), differing, ())


def export_run_manifest(store: RunStore, run_id: str, destination: Path) -> dict[str, object]:
    """Write the immutable Run with its context entities as one JSON manifest."""

    found = store.run(run_id)
    if found is None:
        raise KeyError(run_id)
    run, evidence_sha256 = found
    sample_id = store.sample_id_for_run(run_id)
    manifest = {
        "manifest_schema_version": "1.0.0",
        "kind": "QUALSIGHT_IMMUTABLE_RUN_MANIFEST",
        "run_id": run_id,
        "run_evidence_sha256": evidence_sha256,
        "run": json.loads(run.model_dump_json()),
        "project": json.loads(store.project(run.project_id).model_dump_json()),
        "experiment": json.loads(store.experiment(run.experiment_id).model_dump_json()),
        "sample": json.loads(store.sample(sample_id).model_dump_json()) if sample_id else None,
        "audit_events": [json.loads(event.model_dump_json()) for event in store.audit_events_for(run_id)],
    }
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    manifest["manifest_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return manifest
