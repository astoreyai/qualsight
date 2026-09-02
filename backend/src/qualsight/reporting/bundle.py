"""Reproducibility bundles and reproduction mode (PRD §§41–42; QS-F53).

``export_bundle`` writes a portable ``<run>.qsexp`` (a zip archive) holding
manifest.json, experiment.yaml, protocol.yaml, dataset-manifest.json,
environment.json, metrics.json, results.parquet, figures/, tables/,
checksums.sha256, and README.md for one immutable Run. Restricted biometric
image bytes are never included: the dataset manifest lists sample paths and
hashes only.

``reproduce_bundle`` follows PRD §42 — import → validate dependencies →
validate input hashes → validate model versions → execute → compare outputs —
and reports EXACT, WITHIN_TOLERANCE, DIFFERENT, MISSING_DEPENDENCY,
MISSING_DATA, or BLOCKED from observed evidence. Target results are never
modified to match the reproduction output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
import zipfile

import pyarrow.parquet as pq

from ..domain.run import CompletedRun, ReproductionState
from ..engines.ofiq.adapter import content_id
from ..execution.cli_runtime import USABLE_RESOURCE_STATES
from ..execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    discard_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
from ..matchers.bounded import validate_execution_timeout
from ..provenance import sha256_file

BUNDLE_VERSION = "1.0.0"
NUMERIC_TOLERANCE = 1e-9
VOLATILE_KEYS = {"analysis_id", "created_at", "run_id", "start_time", "end_time", "runtime_seconds", "assessment_ms", "timing", "evidence_sha256", "directory", "generated_at", "note"}
SCORE_TOLERANCE = 1e-5  # same device: float32 embeddings; cosine scores are compared at this absolute tolerance
CROSS_DEVICE_SCORE_TOLERANCE = 1e-3  # original and reproduction ran on different devices (cuda vs cpu kernels); recorded with the result
BUNDLE_ARTIFACT_NAMES = ("bundle.qsexp",)


class BundleError(RuntimeError):
    """Raised when a bundle cannot be written or read faithfully."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _json_yaml(value: Any) -> str:
    """JSON is a subset of YAML 1.2; the .yaml members are written as indented JSON so no extra dependency is needed."""

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"


# -- export ---------------------------------------------------------------------


def _bundle_request_sha256(
    *, run_id: str, run_evidence_sha256: str, project_id: str
) -> str:
    encoded = json.dumps(
        {
            "version": BUNDLE_VERSION,
            "run_id": run_id,
            "run_evidence_sha256": run_evidence_sha256,
            "project_id": project_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def recover_prepared_bundle(
    directory: Path,
    *,
    bundle_id: str,
    project_id: str,
    request_sha256: str,
) -> dict[str, Any]:
    """Validate and recover one fully promoted bundle publication."""

    try:
        receipt = load_prepared_output(
            directory,
            operation_kind="EXPERIMENT_BUNDLE",
            result_id=bundle_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=BUNDLE_ARTIFACT_NAMES,
        )
        record = receipt["record"]
        bundle_path = directory / "bundle.qsexp"
        if (
            set(record)
            != {
                "bundle_id",
                "project_id",
                "run_id",
                "path",
                "sha256",
                "size_bytes",
                "manifest",
                "created_at",
            }
            or record["bundle_id"] != bundle_id
            or record["project_id"] != project_id
            or record["path"] != str(bundle_path)
            or record["sha256"] != sha256_file(bundle_path)
            or record["size_bytes"] != bundle_path.stat().st_size
            or not isinstance(record["manifest"], dict)
            or record["manifest"].get("bundle_version") != BUNDLE_VERSION
            or record["manifest"].get("project_id") != project_id
            or record["manifest"].get("run_id") != record["run_id"]
        ):
            raise BundleError("prepared bundle record contract differs")
        return record
    except (PreparedOutputError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError("prepared bundle failed integrity verification") from error


def export_bundle(
    store: Any,
    *,
    run_id: str,
    root: Path,
    repository_root: Path,
    record_publication: bool = True,
) -> dict[str, Any]:
    found = store.run(run_id)
    if found is None:
        raise BundleError("run is not recorded")
    run, evidence_sha256 = found
    experiment = store.experiment(run.experiment_id)
    project = store.project(run.project_id)
    bundle_id = content_id("bundle", run_id, evidence_sha256, BUNDLE_VERSION)
    directory = root / bundle_id.replace(":", "_")
    request_sha256 = _bundle_request_sha256(
        run_id=run_id,
        run_evidence_sha256=evidence_sha256,
        project_id=run.project_id,
    )
    if directory.exists():
        record = recover_prepared_bundle(
            directory,
            bundle_id=bundle_id,
            project_id=run.project_id,
            request_sha256=request_sha256,
        )
        if record_publication:
            store.record_experiment_bundle(record)
        return record
    stage = create_staging_directory(
        directory,
        operation_kind="EXPERIMENT_BUNDLE",
        result_id=bundle_id,
        project_id=run.project_id,
        request_sha256=request_sha256,
    )
    assembly = stage / ".assembly"
    assembly.mkdir()
    try:
        (assembly / "figures").mkdir()
        (assembly / "tables").mkdir()
        run_json = json.loads(run.model_dump_json())
        (assembly / "experiment.yaml").write_text(_json_yaml({"experiment": json.loads(experiment.model_dump_json()) if experiment else None, "run": run_json, "project": {"project_id": project.project_id, "name": project.name} if project else None}), encoding="utf-8")
        protocol_doc: dict[str, Any] = {"pair_search_protocol": run_json["pair_search_protocol"], "subject_protocol": run_json["subject_protocol"]}
        protocol_id = run.pair_search_protocol.component_id if run.pair_search_protocol.state.value == "CONFIGURED" else None
        if protocol_id:
            protocol = store.protocol(protocol_id)
            if protocol is not None:
                protocol_doc["protocol"] = json.loads(protocol.model_dump_json())
        (assembly / "protocol.yaml").write_text(_json_yaml(protocol_doc), encoding="utf-8")
        dataset_manifest = json.loads(run.dataset_manifest.text)
        (assembly / "dataset-manifest.json").write_text(json.dumps({"dataset_hash": run.dataset_hash, "manifest": dataset_manifest, "note": "sample paths and hashes only; image bytes are never bundled"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (assembly / "environment.json").write_text(json.dumps({"application_version": run.application_version, "repository": run_json["repository"], "environment": run_json["environment"], "hardware": run_json["hardware"], "model_hashes": run_json["model_hashes"], "quality_engine": run_json["quality_engine"], "matcher": run_json["matcher"], "matcher_preprocessing": run_json["matcher_preprocessing"]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        metrics_doc: dict[str, Any] = {"observations": run_json["metrics"], "configuration": json.loads(run.configuration.text)}
        results_copied = None
        for artifact in run.artifacts:
            source = Path(artifact.uri)
            if not source.exists():
                raise BundleError(f"artifact {artifact.uri} is missing")
            if sha256_file(source) != artifact.sha256:
                raise BundleError(f"artifact {artifact.uri} drifted from its recorded hash")
            if source.suffix == ".parquet" and results_copied is None:
                shutil.copyfile(source, assembly / "results.parquet")
                results_copied = artifact.uri
            elif source.suffix == ".json":
                metrics_doc[source.name] = json.loads(source.read_text(encoding="utf-8"))
            elif source.suffix == ".parquet":
                shutil.copyfile(source, assembly / "tables" / source.name)
            elif source.suffix == ".npy":
                shutil.copyfile(source, assembly / "tables" / source.name)
        (assembly / "metrics.json").write_text(json.dumps(metrics_doc, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        for figure in store.figures(project_id=run.project_id):
            if run_id in json.loads(figure["source_run_ids_json"]):
                provenance = json.loads(figure["provenance_json"])
                target = assembly / "figures" / figure["figure_id"].replace(":", "_")
                target.mkdir()
                for name, item in provenance["files"].items():
                    shutil.copyfile(item["path"], target / Path(item["path"]).name)
                (target / "figure.provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (assembly / "README.md").write_text(
            f"# QualSight reproducibility bundle\n\nRun `{run_id}` (evidence sha256 `{evidence_sha256}`), exported {_now()} by QualSight {run.application_version}.\n\n"
            "Contents: manifest.json, experiment.yaml, protocol.yaml, dataset-manifest.json (paths and hashes only — no image bytes), environment.json, metrics.json, results.parquet, figures/, tables/, checksums.sha256.\n\n"
            "Reproduce with `qualsight reproduce-bundle <this file>`: it validates checksums, dependencies, input hashes, and model versions, re-executes the recorded analysis, and reports EXACT, WITHIN_TOLERANCE, DIFFERENT, MISSING_DEPENDENCY, MISSING_DATA, or BLOCKED. Target results are never modified.\n",
            encoding="utf-8",
        )
        manifest = {"bundle_version": BUNDLE_VERSION, "kind": "QUALSIGHT_EXPERIMENT_BUNDLE", "run_id": run_id, "run_evidence_sha256": evidence_sha256, "project_id": run.project_id, "experiment_id": run.experiment_id, "results_source": results_copied,
                    "run_kind": _run_kind(run), "exported_at": _now(), "restricted_images_included": False}
        (assembly / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        checksum_lines = []
        for path in sorted(p for p in assembly.rglob("*") if p.is_file()):
            checksum_lines.append(f"{sha256_file(path)}  {path.relative_to(assembly).as_posix()}")
        (assembly / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
        stage_bundle = stage / "bundle.qsexp"
        with zipfile.ZipFile(stage_bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in assembly.rglob("*") if p.is_file()):
                archive.write(path, path.relative_to(assembly).as_posix())
        shutil.rmtree(assembly)
        bundle_path = directory / "bundle.qsexp"
        record = {"bundle_id": bundle_id, "project_id": run.project_id, "run_id": run_id, "path": str(bundle_path), "sha256": sha256_file(stage_bundle), "size_bytes": stage_bundle.stat().st_size, "manifest": manifest, "created_at": _now()}
        prepare_and_publish(
            stage,
            directory,
            operation_kind="EXPERIMENT_BUNDLE",
            result_id=bundle_id,
            project_id=run.project_id,
            request_sha256=request_sha256,
            artifact_names=BUNDLE_ARTIFACT_NAMES,
            record=record,
        )
    except FileExistsError:
        record = recover_prepared_bundle(
            directory,
            bundle_id=bundle_id,
            project_id=run.project_id,
            request_sha256=request_sha256,
        )
    except PreparedOutputError as error:
        raise BundleError(str(error)) from error
    finally:
        discard_staging_directory(stage)
    if record_publication:
        store.record_experiment_bundle(record)
    return record


def _run_kind(run: CompletedRun) -> str:
    configuration = json.loads(run.configuration.text)
    if configuration.get("kind") in ("QUALITY_UTILITY", "CROSS_MATCHER"):
        return configuration["kind"]
    if run.quality_engine is not None:
        return "QUALITY"
    return "MATCHER"


# -- reproduction ---------------------------------------------------------------


@dataclass
class ReproductionResult:
    reproduction_id: str
    original_run_id: str
    reproduction_run_id: str | None
    state: str
    steps: list[dict[str, Any]]
    differences: list[str]
    compared_values: int
    bundle_sha256: str
    created_at: str


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS and not k.endswith("_run_id") and k != "sources"}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def _compare(left: Any, right: Any, path: str, differences: list[str], counter: list[int], tolerance: float = NUMERIC_TOLERANCE) -> str:
    """Return EXACT / WITHIN_TOLERANCE / DIFFERENT for two stripped documents."""

    if isinstance(left, dict) and isinstance(right, dict):
        state = "EXACT"
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                differences.append(f"{path}.{key}: present on one side only")
                state = "DIFFERENT"
                continue
            child = _compare(left[key], right[key], f"{path}.{key}", differences, counter, tolerance)
            state = _worse(state, child)
        return state
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            differences.append(f"{path}: length {len(left)} vs {len(right)}")
            return "DIFFERENT"
        state = "EXACT"
        for index, (a, b) in enumerate(zip(left, right)):
            state = _worse(state, _compare(a, b, f"{path}[{index}]", differences, counter, tolerance))
        return state
    counter[0] += 1
    if isinstance(left, bool) or isinstance(right, bool) or not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        if left != right:
            differences.append(f"{path}: {left!r} vs {right!r}")
            return "DIFFERENT"
        return "EXACT"
    if left == right:
        return "EXACT"
    scale = max(abs(left), abs(right), 1.0)
    if abs(left - right) <= tolerance * scale:
        return "WITHIN_TOLERANCE"
    differences.append(f"{path}: {left!r} vs {right!r}")
    return "DIFFERENT"


def _worse(a: str, b: str) -> str:
    order = {"EXACT": 0, "WITHIN_TOLERANCE": 1, "DIFFERENT": 2}
    return a if order[a] >= order[b] else b


def reproduce_bundle(bundle_path: Path, *, store: Any, registry: Any, repository_root: Path, work_root: Path, project_service: Any, execution_timeout_seconds: float, engine: Any = None) -> ReproductionResult:
    try:
        execution_timeout_seconds = validate_execution_timeout(
            execution_timeout_seconds
        )
    except ValueError as error:
        raise BundleError(str(error)) from error
    steps: list[dict[str, Any]] = []
    bundle_sha = sha256_file(bundle_path)
    created_at = _now()
    reproduction_id = content_id("reproduction", bundle_sha, created_at)

    def finish(state: str, original: str, differences: list[str] | None = None, compared: int = 0, reproduction_run_id: str | None = None, project_id: str | None = None) -> ReproductionResult:
        result = ReproductionResult(reproduction_id, original, reproduction_run_id, state, steps, differences or [], compared, bundle_sha, created_at)
        store.record_reproduction({**asdict(result), "project_id": project_id})
        return result

    # Import + checksums.
    with zipfile.ZipFile(bundle_path) as archive:
        names = set(archive.namelist())
        required = {"manifest.json", "experiment.yaml", "protocol.yaml", "dataset-manifest.json", "environment.json", "metrics.json", "checksums.sha256", "README.md"}
        missing = sorted(required - names)
        if missing:
            steps.append({"step": "import", "ok": False, "missing": missing})
            return finish(ReproductionState.BLOCKED.value, "unknown")
        checksums = {}
        for line in archive.read("checksums.sha256").decode("utf-8").splitlines():
            digest, _, member = line.partition("  ")
            checksums[member] = digest
        bad = [member for member, digest in checksums.items() if member in names and hashlib.sha256(archive.read(member)).hexdigest() != digest]
        steps.append({"step": "validate-checksums", "ok": not bad, "members": len(checksums), "mismatched": bad})
        manifest = json.loads(archive.read("manifest.json"))
        experiment_doc = json.loads(archive.read("experiment.yaml"))
        environment_doc = json.loads(archive.read("environment.json"))
        metrics_doc = json.loads(archive.read("metrics.json"))
    original_run_id = manifest["run_id"]
    if bad:
        return finish(ReproductionState.BLOCKED.value, original_run_id)
    run_json = experiment_doc["run"]
    project_id = run_json["project_id"]
    # Validate dependencies: registry states of every bound resource must be usable.
    resource_ids = []
    if run_json.get("quality_engine"):
        resource_ids.append(run_json["quality_engine"]["resource_id"])
    matcher_component = run_json["matcher"].get("component_id")
    if matcher_component and matcher_component.endswith("-local-candidate"):
        resource_ids.append(matcher_component)
    blocked = []
    for resource_id in resource_ids:
        try:
            state = registry.raw_resource(resource_id)["state"]
        except Exception:  # noqa: BLE001 - unknown resource is a missing dependency
            steps.append({"step": "validate-dependencies", "ok": False, "missing_resource": resource_id})
            return finish(ReproductionState.MISSING_DEPENDENCY.value, original_run_id, project_id=project_id)
        if state not in USABLE_RESOURCE_STATES:
            blocked.append({"resource_id": resource_id, "state": state})
    steps.append({"step": "validate-dependencies", "ok": not blocked, "resources": resource_ids, "blocked": blocked})
    if blocked:
        return finish(ReproductionState.BLOCKED.value, original_run_id, project_id=project_id)
    # Validate model versions: every recorded model hash must exist byte-identically.
    missing_models = [identity["path"] for identity in run_json["model_hashes"] if not Path(identity["path"]).exists() or sha256_file(Path(identity["path"])) != identity["sha256"]]
    steps.append({"step": "validate-model-versions", "ok": not missing_models, "models": len(run_json["model_hashes"]), "missing_or_drifted": missing_models})
    if missing_models:
        return finish(ReproductionState.MISSING_DEPENDENCY.value, original_run_id, project_id=project_id)
    # Validate input hashes: the source Runs / artifacts the analysis reads must be recorded with the recorded hashes.
    kind = manifest.get("run_kind")
    configuration = metrics_doc["configuration"]
    if kind in ("QUALITY_UTILITY", "CROSS_MATCHER"):
        sources = configuration["sources"]
        checks = []
        if kind == "QUALITY_UTILITY":
            leaf = store.matcher_leaf(sources["matcher_run_id"])
            job = store.job(sources["quality_job_id"])
            checks.append(("matcher_run", leaf is not None and leaf["comparisons_sha256"] == sources["comparisons_sha256"] and Path(leaf["comparisons_parquet"]).exists()))
            checks.append(("quality_job", job is not None and job.get("parquet_path") and Path(job["parquet_path"]).exists() and sha256_file(Path(job["parquet_path"])) == sources["quality_parquet_sha256"]))
        else:
            for leaf_ref in sources["leaves"]:
                leaf = store.matcher_leaf(leaf_ref["run_id"])
                checks.append((leaf_ref["leaf_id"], leaf is not None and leaf["comparisons_sha256"] == leaf_ref["comparisons_sha256"] and Path(leaf["comparisons_parquet"]).exists()))
            job = store.job(sources["quality_job_id"])
            checks.append(("quality_job", job is not None and job.get("parquet_path") and Path(job["parquet_path"]).exists() and sha256_file(Path(job["parquet_path"])) == sources["quality_parquet_sha256"]))
        failed = [name for name, ok in checks if not ok]
        steps.append({"step": "validate-input-hashes", "ok": not failed, "inputs": [name for name, _ in checks], "missing": failed})
        if failed:
            return finish(ReproductionState.MISSING_DATA.value, original_run_id, project_id=project_id)
        # Execute into a scratch job root and compare.
        project = project_service.require_writable(project_id)
        work_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="reproduce-", dir=str(work_root)))
        try:
            if kind == "QUALITY_UTILITY":
                from ..analysis.utility import UtilityAnalysisJob, UtilityRequest

                request = UtilityRequest(matcher_run_id=sources["matcher_run_id"], quality_job_id=sources["quality_job_id"], quality_measure=sources["quality_measure"], pair_quality_policy=configuration["pair_quality_policy"]["id"],
                                         target_fmr=configuration["matcher_threshold"].get("target_fmr", 0.001), matcher_threshold=configuration["matcher_threshold"]["value"] if configuration["matcher_threshold"].get("source") == "CALLER_SUPPLIED" else None,
                                         quality_threshold=configuration["quality_policy_threshold"]["value"] if configuration["quality_policy_threshold"].get("source") == "CALLER_SUPPLIED" else None,
                                         partial_interval=configuration["rejection_domain"]["partial_interval"][1], bootstrap_replicates=configuration["uncertainty"]["replicates"], seed=configuration["uncertainty"]["seed"])
                job = UtilityAnalysisJob(store=_NonRecordingStore(store), repository_root=repository_root, project=project, actor="reproduction", request=request, job_root=scratch)
                result = job.run()
                produced = {"utility.json": json.loads(result.utility_path.read_text(encoding="utf-8")), "policy.json": json.loads(result.policy_path.read_text(encoding="utf-8"))}
                reproduction_run_id = result.run.run_id
            else:
                from ..analysis.crossmatcher import CrossMatcherJob, CrossMatcherRequest

                request = CrossMatcherRequest(matrix_id=sources["matrix_id"], quality_job_id=sources["quality_job_id"], quality_measure=sources["quality_measure"], pair_quality_policy=configuration["pair_quality_policy"]["id"], target_fmr=configuration["target_fmr"], partial_interval=configuration["partial_interval"], bootstrap_replicates=configuration["uncertainty"]["replicates"], seed=configuration["uncertainty"]["seed"])
                job = CrossMatcherJob(store=_NonRecordingStore(store), registry=registry, repository_root=repository_root, project=project, actor="reproduction", request=request, job_root=scratch)
                result = job.run()
                produced = {"crossmatcher.json": json.loads(result.document_path.read_text(encoding="utf-8"))}
                reproduction_run_id = result.run.run_id
            differences: list[str] = []
            counter = [0]
            state = "EXACT"
            for name, produced_doc in produced.items():
                target = metrics_doc.get(name)
                if target is None:
                    differences.append(f"{name}: not present in the bundle")
                    state = "DIFFERENT"
                    continue
                state = _worse(state, _compare(_strip_volatile(target), _strip_volatile(produced_doc), name, differences, counter))
            steps.append({"step": "execute-and-compare", "ok": True, "kind": kind, "compared_values": counter[0], "state": state, "reproduction_run_id": reproduction_run_id})
            return finish(state, original_run_id, differences, counter[0], reproduction_run_id, project_id)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if kind == "MATCHER":
        return _reproduce_matcher(run_json, configuration, metrics_doc, bundle_path, store, registry, repository_root, work_root, project_service, steps, finish, original_run_id, project_id, execution_timeout_seconds)
    if kind == "QUALITY":
        return _reproduce_batch(run_json, configuration, metrics_doc, bundle_path, store, registry, repository_root, work_root, project_service, steps, finish, original_run_id, project_id, engine)
    steps.append({"step": "execute", "ok": False, "kind": kind, "reason": "unknown Run kind"})
    return finish(ReproductionState.BLOCKED.value, original_run_id, project_id=project_id)


def _bundle_results(bundle_path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(bundle_path) as archive, tempfile.TemporaryDirectory() as scratch:
        archive.extract("results.parquet", scratch)
        return pq.read_table(Path(scratch) / "results.parquet").to_pylist()


def _reproduce_matcher(run_json, configuration, metrics_doc, bundle_path, store, registry, repository_root, work_root, project_service, steps, finish, original_run_id, project_id, execution_timeout_seconds):
    from ..domain.entities import Dataset  # noqa: F401 - typing aid
    from ..matchers.conditions import ConditionId
    from ..matchers.matrix import MatcherMatrixJob, MatrixError
    from ..matchers.runtime import MatcherId
    from ..matchers.upstream import UpstreamSources

    protocol_id = configuration["protocol_id"]
    protocol = store.protocol(protocol_id)
    leaf = store.matcher_leaf(original_run_id)
    if protocol is None or leaf is None:
        steps.append({"step": "validate-input-hashes", "ok": False, "missing": ["protocol or leaf record"]})
        return finish(ReproductionState.MISSING_DATA.value, original_run_id, project_id=project_id)
    dataset = store.dataset(leaf["dataset_id"])
    pairs = store.protocol_pairs(protocol_id, limit=1_000_000)
    target_rows = _bundle_results(bundle_path)
    wanted = {row["pair_id"] for row in target_rows}
    pairs = [pair for pair in pairs if pair.pair_id in wanted]
    missing_samples = []
    for pair in pairs:
        for sample_id in (pair.left_sample_id, pair.right_sample_id):
            sample = store.sample(sample_id)
            if sample is None or not Path(sample.source_path).exists() or sha256_file(Path(sample.source_path)) != sample.sha256:
                missing_samples.append(sample_id)
    steps.append({"step": "validate-input-hashes", "ok": not missing_samples, "pairs": len(pairs), "samples_missing_or_drifted": sorted(set(missing_samples))[:20]})
    if missing_samples or len(pairs) != len(target_rows):
        return finish(ReproductionState.MISSING_DATA.value, original_run_id, project_id=project_id)
    matcher_id = MatcherId(configuration["matcher"]["resource_id"])
    condition = ConditionId(configuration["preprocessing"]["condition_id"])
    project = project_service.require_writable(project_id)
    upstream = UpstreamSources(repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json", work_root.parent)
    upstream.acquire()
    work_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="reproduce-matcher-", dir=str(work_root)))
    try:
        try:
            job = MatcherMatrixJob(registry=registry, registry_path=repository_root / "resources" / "resource-registry.json", repository_root=repository_root, upstream=upstream, store=_NonRecordingStore(store), project=project, dataset=dataset, protocol=protocol, pairs=pairs,
                                   job_root=scratch, matchers=(matcher_id,), conditions=(condition,), selection_rule="reproduction of a bundled Run: the bundled pair set", purpose_scope=configuration.get("purpose_scope", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"), execution_timeout_seconds=execution_timeout_seconds)
        except MatrixError as error:
            steps.append({"step": "execute", "ok": False, "reason": str(error)})
            return finish(ReproductionState.BLOCKED.value, original_run_id, project_id=project_id)
        leaves = job.run()
        produced_rows = pq.read_table(leaves[0].comparisons_parquet).to_pylist()
        original_device = (configuration.get("matcher") or {}).get("device")
        reproduction_device = json.loads(leaves[0].run.configuration.text).get("matcher", {}).get("device")
        cross_device = original_device is not None and reproduction_device is not None and original_device != reproduction_device
        score_tolerance = CROSS_DEVICE_SCORE_TOLERANCE if cross_device else SCORE_TOLERANCE
        metric_tolerance = 1e-3 if cross_device else 1e-6
        differences: list[str] = []
        counter = [0]
        by_pair = {row["pair_id"]: row for row in produced_rows}
        state = "EXACT"
        for row in target_rows:
            other = by_pair.get(row["pair_id"])
            if other is None:
                differences.append(f"{row['pair_id']}: missing in reproduction")
                state = "DIFFERENT"
                continue
            for column in ("label", "failure", "failure_side"):
                counter[0] += 1
                if row[column] != other[column]:
                    differences.append(f"{row['pair_id']}.{column}: {row[column]!r} vs {other[column]!r}")
                    state = "DIFFERENT"
            counter[0] += 1
            if row["score"] is None or other["score"] is None:
                if row["score"] != other["score"]:
                    differences.append(f"{row['pair_id']}.score: {row['score']!r} vs {other['score']!r}")
                    state = "DIFFERENT"
            elif row["score"] != other["score"]:
                if abs(row["score"] - other["score"]) <= score_tolerance:
                    state = _worse(state, "WITHIN_TOLERANCE")
                else:
                    differences.append(f"{row['pair_id']}.score: {row['score']!r} vs {other['score']!r}")
                    state = "DIFFERENT"
        produced_metrics = json.loads(json.dumps(leaves[0].metrics, sort_keys=True, allow_nan=False))
        metric_state = _compare(_strip_volatile(metrics_doc.get("verification_metrics.json", {})), _strip_volatile(produced_metrics), "verification_metrics.json", differences, counter, tolerance=metric_tolerance)
        state = _worse(state, metric_state)
        steps.append({"step": "execute-and-compare", "ok": True, "kind": "MATCHER", "compared_values": counter[0], "state": state, "score_tolerance": score_tolerance, "metric_tolerance": metric_tolerance, "original_device": original_device, "reproduction_device": reproduction_device, "cross_device": cross_device, "tolerance_basis": "CROSS_DEVICE" if cross_device else "SAME_DEVICE", "reproduction_run_id": leaves[0].run.run_id})
        return finish(state, original_run_id, differences[:200], counter[0], leaves[0].run.run_id, project_id)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _reproduce_batch(run_json, configuration, metrics_doc, bundle_path, store, registry, repository_root, work_root, project_service, steps, finish, original_run_id, project_id, engine):
    from ..api.settings import Settings
    from ..engines.ofiq.adapter import OFIQQualityEngine
    from ..engines.ofiq.batch import BatchJobError, OFIQBatchJob

    target_rows = _bundle_results(bundle_path)
    samples = []
    missing = []
    for row in target_rows:
        sample = store.sample(row["sample_id"])
        if sample is None or not Path(sample.source_path).exists() or sha256_file(Path(sample.source_path)) != sample.sha256 or sample.sha256 != row["sha256"]:
            missing.append(row["sample_id"])
        else:
            samples.append(sample)
    steps.append({"step": "validate-input-hashes", "ok": not missing, "samples": len(target_rows), "missing_or_drifted": missing[:20]})
    if missing:
        return finish(ReproductionState.MISSING_DATA.value, original_run_id, project_id=project_id)
    if engine is None:
        settings = Settings.from_environment(repository_root)
        engine = OFIQQualityEngine(registry_path=settings.registry_path, runtime_resource_id=settings.runtime_resource_id, input_resource_ids=settings.input_resource_ids, repository_root=repository_root)
    project = project_service.require_writable(project_id)
    dataset = store.dataset(samples[0].dataset_id)
    work_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="reproduce-batch-", dir=str(work_root)))
    try:
        try:
            job = OFIQBatchJob(engine=engine, store=_NonRecordingStore(store), project=project, dataset=dataset, samples=samples, purpose_scope=configuration.get("purpose_scope", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"), job_root=scratch, chunks=1, selection_rule="reproduction of a bundled Run: the bundled sample set")
        except BatchJobError as error:
            steps.append({"step": "execute", "ok": False, "reason": str(error)})
            return finish(ReproductionState.BLOCKED.value, original_run_id, project_id=project_id)
        job.run()
        produced = {row["sample_id"]: row for row in pq.read_table(job.parquet_path).to_pylist()}
        differences: list[str] = []
        counter = [0]
        state = "EXACT"
        for row in target_rows:
            other = produced.get(row["sample_id"])
            if other is None:
                differences.append(f"{row['sample_id']}: missing in reproduction")
                state = "DIFFERENT"
                continue
            for column, value in row.items():
                if column in ("chunk", "assessment_ms", "source_path"):
                    continue
                counter[0] += 1
                child = _compare(value, other.get(column), f"{row['sample_id']}.{column}", differences, counter, tolerance=1e-9)
                counter[0] -= 1
                state = _worse(state, child)
        steps.append({"step": "execute-and-compare", "ok": True, "kind": "QUALITY", "compared_values": counter[0], "state": state, "reproduction_job_id": job.job_id})
        return finish(state, original_run_id, differences[:200], counter[0], None, project_id)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class _NonRecordingStore:
    """Read-through view of the store that keeps reproduction Runs out of the immutable record tables."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in {"record_utility_analysis", "record_cross_matcher_analysis"}:
            return lambda result: None
        if name in {"record_matcher_leaf", "record_batch_job"}:
            return lambda *args, **kwargs: None
        return getattr(self._inner, name)
