"""QualSight backend command line: assess, reproduce, export.

    python -m qualsight.cli assess --image <registered path> --scope <scope>
    python -m qualsight.cli reproduce --run-id <run_id>
    python -m qualsight.cli export-run --run-id <run_id> --out <file.json>
    python -m qualsight.cli serve
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .api.settings import Settings
from .api.store import RunStore
from .domain.primitives import CanonicalJson
from .engines.ofiq.adapter import OFIQQualityEngine
from .reproduction import export_run_manifest, reproduce_run
from .security.local_storage import activate_private_umask, secure_data_tree
from .security.network_guard import install_network_guard, permit_explicit_setup_network
from .security.process_lease import DataDirectoryLease, DataDirectoryLeaseError


def _engine(settings: Settings) -> OFIQQualityEngine:
    return OFIQQualityEngine(
        registry_path=settings.registry_path,
        repository_root=settings.repository_root,
        runtime_resource_id=settings.runtime_resource_id,
        input_resource_ids=settings.input_resource_ids,
    )


def _selected_axis(values: list[str] | None, enum_type):
    """Resolve repeatable CLI choices to one ordered catalog axis."""

    selected = (enum_type(value) for value in values) if values else iter(enum_type)
    return tuple(dict.fromkeys(selected))


def _run_quality_batch_operation(
    store: RunStore,
    job: Any,
    request: dict[str, Any],
) -> Any:
    """Execute one CLI quality batch behind the durable operation ledger."""

    operation, created = store.admit_operation(
        operation_id=job.job_id,
        operation_kind="QUALITY_BATCH",
        project_id=job.project.project_id,
        request=request,
        output_path=job.job_dir,
        result_id=job.job_id,
    )
    if not created:
        raise RuntimeError("CLI quality operation identity is already recorded")
    job.bind_operation_request_sha256(CanonicalJson.from_value(request).sha256)
    try:
        store.start_operation(operation["operation_id"])
        progress = job.run()
    except BaseException:
        current = store.operation(operation["operation_id"])
        if current is not None and current["state"] in {"QUEUED", "RUNNING"}:
            store.finish_operation(
                operation["operation_id"],
                state="FAILED",
                error=job.error or "CLI quality batch execution failed",
            )
        raise
    else:
        store.finish_operation(operation["operation_id"], state="COMPLETED")
        return progress
    finally:
        store.finish_job(
            job.job_id,
            job.ended_at.isoformat() if job.ended_at else "",
            job.state,
        )


def _run_external_score_operation(store: RunStore, importer: Any) -> Any:
    """Import registered scores behind the durable operation ledger."""

    request = importer.operation_request
    operation, created = store.admit_operation(
        operation_id=importer.matrix_id,
        operation_kind="EXTERNAL_SCORE_IMPORT",
        project_id=importer.project.project_id,
        request=request,
        output_path=importer.job_dir,
        result_id=importer.matrix_id,
    )
    if not created:
        raise RuntimeError("CLI external-score operation identity is already recorded")
    request_sha256 = CanonicalJson.from_value(request).sha256
    try:
        store.start_operation(operation["operation_id"])
        result = importer.run(
            request_sha256=request_sha256,
            operation_id=operation["operation_id"],
        )
    except BaseException as error:
        current = store.operation(operation["operation_id"])
        if current is not None and current["state"] in {"QUEUED", "RUNNING"}:
            store.finish_operation(
                operation["operation_id"],
                state="FAILED",
                error=f"{type(error).__name__}: {error}",
            )
        raise
    else:
        completed = store.operation(operation["operation_id"])
        if completed is None or completed["state"] != "COMPLETED":
            raise RuntimeError(
                "CLI external-score result did not complete its durable operation"
            )
        return result


def main(argv: list[str] | None = None) -> int:
    from .matchers.conditions import ConditionId
    from .matchers.runtime import MatcherId

    parser = argparse.ArgumentParser(prog="qualsight")
    sub = parser.add_subparsers(dest="command", required=True)
    assess = sub.add_parser("assess", help="assess one registered image with the registered engine")
    assess.add_argument("--image", required=True)
    assess.add_argument("--scope", required=True)
    reproduce = sub.add_parser("reproduce", help="re-execute a recorded Run and attest the outcome")
    reproduce.add_argument("--run-id", required=True)
    reproduce.add_argument("--no-record", action="store_true", help="do not persist the reproduction Run")
    export = sub.add_parser("export-run", help="export an immutable Run manifest")
    export.add_argument("--run-id", required=True)
    export.add_argument("--out", type=Path, required=True)
    snapshot = sub.add_parser(
        "snapshot-database",
        help="create a consistent private SQLite snapshot for an independent verifier or audit",
    )
    snapshot.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new no-clobber path ending in .snapshot.sqlite3",
    )
    backup_create = sub.add_parser(
        "backup-create",
        help="create a no-clobber offline backup while exclusively owning the data directory",
    )
    backup_create.add_argument("--output", type=Path, required=True)
    backup_verify = sub.add_parser(
        "backup-verify",
        help="verify every hash and the SQLite integrity of an existing backup",
    )
    backup_verify.add_argument("--backup", type=Path, required=True)
    backup_restore = sub.add_parser(
        "backup-restore",
        help="restore a verified backup to its recorded, currently absent data directory",
    )
    backup_restore.add_argument("--backup", type=Path, required=True)
    sub.add_parser("serve", help="run the local control plane on 127.0.0.1:8480")
    project_create = sub.add_parser("project-create", help="create a project (lifecycle NEW)")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--description", required=True)
    project_create.add_argument("--scope", required=True)
    import_lfw = sub.add_parser("import-lfw", help="import the registered LFW dataset into a project")
    import_lfw.add_argument("--project-id", required=True)
    import_lfw.add_argument("--skip-hash-verification", action="store_true")
    batch = sub.add_parser("batch", help="run the registered OFIQ engine over recorded dataset samples")
    batch.add_argument("--project-id", required=True)
    batch.add_argument("--dataset-id", required=True)
    batch.add_argument("--selection", default="all", help="all | subset_fixture | subject:<id>")
    batch.add_argument("--chunks", type=int, default=1)
    batch.add_argument("--scope", default="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1")
    batch.add_argument("--execution", default="host", choices=["host", "container"], help="run the registered engine on the host or inside the QualSight-built network-less container (QS-X64)")
    freeze = sub.add_parser("freeze-lfw-protocol", help="freeze the registered LFW View 2 protocol over an imported dataset")
    freeze.add_argument("--project-id", required=True)
    freeze.add_argument("--dataset-id", required=True)
    matrix = sub.add_parser("matrix", help="run a selected matcher × preprocessing product over a frozen protocol")
    matrix.add_argument("--project-id", required=True)
    matrix.add_argument("--protocol-id", required=True)
    matrix.add_argument("--fixture-pairs-only", action="store_true", help="restrict to the registered subset-fixture pairs (gate mode)")
    matrix.add_argument("--matcher", dest="matcher_ids", action="append", choices=[item.value for item in MatcherId], help="admitted matcher id; repeat to select multiple (default: all)")
    matrix.add_argument("--preprocessing", dest="condition_ids", action="append", choices=[item.value for item in ConditionId], help="admitted preprocessing profile id; repeat to select multiple (default: all)")
    utility = sub.add_parser("utility", help="run a quality-utility analysis over one matcher Run and one quality job")
    utility.add_argument("--project-id", required=True)
    utility.add_argument("--matcher-run-id", required=True)
    utility.add_argument("--quality-job-id", required=True)
    utility.add_argument("--quality-measure", default="UnifiedQualityScore")
    utility.add_argument("--policy", default="pair.quality.min")
    utility.add_argument("--target-fmr", type=float, default=0.001)
    utility.add_argument("--matcher-threshold", type=float, default=None)
    utility.add_argument("--quality-threshold", type=float, default=None)
    utility.add_argument("--partial-interval", type=float, default=0.2)
    utility.add_argument("--replicates", type=int, default=1000)
    utility.add_argument("--seed", type=int, default=20260817)
    cross = sub.add_parser("cross-matcher", help="run fixed-condition matcher contrasts and fixed-matcher preprocessing contrasts over a recorded matrix")
    cross.add_argument("--project-id", required=True)
    cross.add_argument("--matrix-id", required=True)
    cross.add_argument("--quality-job-id", required=True)
    cross.add_argument("--quality-measure", default="UnifiedQualityScore")
    cross.add_argument("--policy", default="pair.quality.min")
    cross.add_argument("--target-fmr", type=float, default=0.001)
    cross.add_argument("--partial-interval", type=float, default=0.2)
    cross.add_argument("--replicates", type=int, default=1000)
    cross.add_argument("--seed", type=int, default=20260817)
    bundle = sub.add_parser("bundle-export", help="export a portable .qsexp reproducibility bundle for one immutable Run")
    bundle.add_argument("--run-id", required=True)
    repro = sub.add_parser("reproduce-bundle", help="validate and re-execute a .qsexp bundle; report EXACT/WITHIN_TOLERANCE/DIFFERENT/MISSING_DEPENDENCY/MISSING_DATA/BLOCKED")
    repro.add_argument("bundle", type=Path)
    figure = sub.add_parser("figure", help="render a provenance-bearing figure (PNG/SVG/PDF/CSV + provenance sidecar) from recorded Runs")
    figure.add_argument("--project-id", required=True)
    figure.add_argument("--kind", required=True)
    figure.add_argument("--params", default="{}", help="JSON object of builder parameters")
    setup = sub.add_parser("setup", help="installation-time acquisition of pinned upstream matcher sources into the data directory (the only sanctioned network use); verifies the registry afterwards")
    setup.add_argument("--verify-registry", action="store_true", help="also stream-verify every registered artifact hash (slow for LFW)")
    ext = sub.add_parser("import-external-scores", help="import a registered externally computed score artifact over a frozen protocol (QS-C21)")
    ext.add_argument("--project-id", required=True)
    ext.add_argument("--protocol-id", required=True)
    ext.add_argument("--resource-id", required=True)
    owner_assess = sub.add_parser("assess-owner", help="admit one operator-supplied local photo (decision 30) and assess it with the registered engine as a recorded Run")
    owner_assess.add_argument("--project-id", required=True)
    owner_assess.add_argument("--image", required=True)
    owner_assess.add_argument("--scope", default="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1")
    owner_dir = sub.add_parser("import-owner-directory", help="import an operator directory as an identity-labelled owner-supplied dataset (subject = immediate folder name)")
    owner_dir.add_argument("--project-id", required=True)
    owner_dir.add_argument("--root", required=True)
    owner_dir.add_argument("--name", required=True)
    owner_pairs = sub.add_parser("freeze-owner-pairs", help="freeze a deterministic seeded pair protocol over an owner-supplied dataset")
    owner_pairs.add_argument("--project-id", required=True)
    owner_pairs.add_argument("--dataset-id", required=True)
    owner_pairs.add_argument("--seed", type=int, required=True)
    owner_pairs.add_argument("--max-mated-per-subject", type=int, default=6)
    owner_pairs.add_argument("--nonmated-multiple", type=float, default=1.0)
    ident = sub.add_parser("identification", help="freeze the LFW 1:N protocol (if needed) and run an open-set search with a frozen matcher on the GPU (QS-X60)")
    ident.add_argument("--project-id", required=True)
    ident.add_argument("--dataset-id", required=True)
    ident.add_argument("--matcher-id", required=True)
    ident.add_argument("--condition-id", default="SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1")
    ident.add_argument("--selection", default="all", choices=["all", "subset_fixture"])
    ident.add_argument("--rank-depth", type=int, default=20)
    ident.add_argument("--quality-job-id", default=None)
    args = parser.parse_args(argv)

    settings = Settings.from_environment()
    activate_private_umask()
    if settings.network_guard:
        install_network_guard()
    if args.command == "backup-verify":
        from .security.backup import verify_backup

        print(json.dumps(verify_backup(args.backup), sort_keys=True))
        return 0
    if args.command == "backup-restore":
        from .security.backup import restore_backup

        print(json.dumps(restore_backup(args.backup, settings.data_dir), sort_keys=True))
        return 0
    secure_data_tree(settings.data_dir, protected_roots=(settings.repository_root, Path.home()))
    if args.command == "serve":
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=8480, log_level="info")
        return 0

    try:
        lease = DataDirectoryLease.acquire(settings.data_dir, owner=f"cli:{args.command}")
    except DataDirectoryLeaseError as error:
        print(json.dumps({"error": "DATA_DIRECTORY_BUSY", "detail": str(error)}, sort_keys=True), file=sys.stderr)
        return 4
    try:
        store = RunStore(settings.database_path)
    except BaseException:
        lease.close()
        raise
    try:
        store.reconcile_prepared_owner_publications(settings.data_dir)
        store.reconcile_interrupted_operations()
        from .reporting.recovery import (
            reconcile_prepared_reporting_publications,
            reporting_publication_audit_event,
        )

        reconcile_prepared_reporting_publications(
            store,
            data_dir=settings.data_dir,
            actor_id=settings.actor,
        )
        if args.command == "snapshot-database":
            print(json.dumps(store.snapshot_database(args.output), sort_keys=True))
            return 0
        if args.command == "backup-create":
            from .security.backup import create_backup

            print(
                json.dumps(
                    create_backup(
                        store,
                        settings.data_dir,
                        args.output,
                        lease=lease,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "assess":
            engine = _engine(settings)
            result = engine.assess(args.image, args.scope)
            if not result.succeeded:
                print(json.dumps({"error": "ASSESSMENT_FAILED", "reason": result.row.failure_reason}, indent=2))
                return 3
            bundle = engine.build_run(result, purpose_scope=args.scope)
            evidence = store.record_bundle(bundle)
            print(
                json.dumps(
                    {
                        "run_id": bundle.run.run_id,
                        "evidence_sha256": evidence,
                        "engine_version": bundle.run.quality_engine.version,
                        "metrics": {m.metric_id: m.value for m in bundle.run.metrics},
                        "failed_measures": list(result.row.failed_measure_names),
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "reproduce":
            engine = _engine(settings)
            report = reproduce_run(
                store=store, engine=engine, run_id=args.run_id, record_reproduction=not args.no_record
            )
            print(
                json.dumps(
                    {
                        "original_run_id": report.original_run_id,
                        "reproduction_run_id": report.reproduction_run_id,
                        "state": report.state.value,
                        "compared_metrics": report.compared_metrics,
                        "differing_metrics": list(report.differing_metrics),
                        "missing": list(report.missing),
                        "attestation": json.loads(report.attestation.model_dump_json()),
                    },
                    indent=2,
                )
            )
            return 0 if report.state.value == "EXACT" else 4
        if args.command == "project-create":
            from .projects import ProjectService

            engine = _engine(settings)
            view = ProjectService(store, engine).create(name=args.name, description=args.description, purpose_scope=args.scope)
            print(json.dumps({"project_id": view.project.project_id, "lifecycle": view.project.lifecycle.value, "version": view.version}, indent=2))
            return 0
        if args.command == "import-lfw":
            from .datasets import RegisteredDatasetImporter
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            result = RegisteredDatasetImporter(engine.registry, store).import_lfw(project, verify_hashes=not args.skip_hash_verification)
            print(json.dumps({"dataset_id": result.dataset.dataset_id, "samples": result.sample_count, "subjects": result.subject_count, "statistics_sha256": result.statistics_sha256}, indent=2))
            return 0
        if args.command == "batch":
            from .engines.ofiq.batch import OFIQBatchJob
            from .projects import ProjectService

            engine = _engine(settings)
            project = ProjectService(store, engine).require_writable(args.project_id)
            dataset = store.dataset(args.dataset_id)
            if dataset is None:
                raise SystemExit("dataset is not recorded")
            if args.selection == "all":
                samples = store.samples_for_dataset(args.dataset_id, limit=1_000_000)
                rule = "all recorded samples of the dataset in source-path order"
            elif args.selection == "subset_fixture":
                fixture = json.loads((settings.repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
                tree_root = json.loads(dataset.manifest.text)["tree_root"]
                samples = [store.sample_by_path(args.dataset_id, f"{tree_root}/{image['path']}") for image in fixture["images"]]
                rule = f"registered subset fixture {fixture['fixture_id']}"
            elif args.selection.startswith("subject:"):
                samples = store.samples_for_dataset(args.dataset_id, limit=1_000_000, subject_id=args.selection.split(":", 1)[1])
                rule = f"all recorded samples of subject {args.selection.split(':', 1)[1]}"
            else:
                raise SystemExit(f"unsupported selection {args.selection}")
            job = OFIQBatchJob(
                engine=engine, store=store, project=project, dataset=dataset, samples=[s for s in samples if s is not None],
                purpose_scope=args.scope, job_root=settings.data_dir / "jobs", chunks=args.chunks, selection_rule=rule,
                execution=args.execution,
            )
            print(json.dumps({"job_id": job.job_id, "total": len(job.samples), "chunks_requested": job.chunks_requested, "job_dir": str(job.job_dir)}, indent=2), file=sys.stderr, flush=True)
            operation_request = {
                "interface": "CLI",
                "command": "batch",
                "dataset_id": args.dataset_id,
                "selection": args.selection,
                "chunks": args.chunks,
                "purpose_scope": args.scope,
                "execution": args.execution,
                "sample_count": len(job.samples),
                "selection_rule": rule,
            }
            progress = _run_quality_batch_operation(store, job, operation_request)
            print(json.dumps({"job_id": progress.job_id, "state": progress.state, "assessed": progress.assessed, "failed": progress.failed, "integrity_failures": progress.integrity_failures, "not_assessed": progress.not_assessed, "run_id": progress.run_id, "resource_use": progress.resource_use, "parquet_path": str(job.parquet_path)}, indent=2))
            return 0 if progress.state == "COMPLETED" else 5
        if args.command == "freeze-lfw-protocol":
            from .projects import ProjectService
            from .protocols import ProtocolService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            dataset = store.dataset(args.dataset_id)
            if dataset is None:
                raise SystemExit("dataset is not recorded")
            frozen = ProtocolService(engine.registry, store, service.actor).freeze_lfw_view2(project, dataset)
            print(json.dumps({"protocol_id": frozen.record.protocol_id, "pairs": frozen.record.pair_count, "mated": frozen.record.mated_count, "pairs_sha256": frozen.record.pairs_sha256}, indent=2))
            return 0
        if args.command == "matrix":
            from .matchers.matrix import MatcherMatrixJob
            from .matchers.runtime import UPSTREAM_KEYS
            from .matchers.upstream import UpstreamSources
            from .projects import ProjectService

            engine = _engine(settings)
            project = ProjectService(store, engine).require_writable(args.project_id)
            protocol = store.protocol(args.protocol_id)
            if protocol is None:
                raise SystemExit("protocol is not recorded")
            dataset = store.dataset(protocol.dataset_id)
            pairs = store.protocol_pairs(args.protocol_id, limit=1_000_000)
            rule = "every pair of the frozen protocol"
            if args.fixture_pairs_only:
                fixture = json.loads((settings.repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
                wanted = {p["pair_id"] for p in fixture["pairs"]}
                pairs = [pair for pair in pairs if pair.pair_id in wanted]
                rule = "the registered subset-fixture pairs of the frozen protocol"
            selected_matchers = _selected_axis(args.matcher_ids, MatcherId)
            selected_conditions = _selected_axis(args.condition_ids, ConditionId)
            selected_leaves = [f"{matcher.value} × {condition.value}" for matcher in selected_matchers for condition in selected_conditions]
            rule += f" (selected matcher x preprocessing product: {selected_leaves})"
            upstream = UpstreamSources(settings.repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json", settings.data_dir, cache_root=settings.upstream_cache_root)
            try:
                for matcher_id in selected_matchers:
                    for key in UPSTREAM_KEYS[matcher_id]:
                        upstream.verify(key)
            except Exception as error:  # noqa: BLE001 - actionable dependency refusal at the CLI boundary
                raise SystemExit(f"matcher architecture sources are not ready; run `qualsight setup` explicitly: {error}") from error
            job = MatcherMatrixJob(
                registry=engine.registry, registry_path=settings.registry_path, repository_root=settings.repository_root, upstream=upstream,
                store=store, project=project, dataset=dataset, protocol=protocol, pairs=pairs, job_root=settings.data_dir / "matrix", selection_rule=rule,
                matchers=selected_matchers, conditions=selected_conditions,
                execution_timeout_seconds=settings.native_execution_timeout_seconds,
            )
            print(json.dumps({"matrix_id": job.matrix_id, "pairs": len(job.pairs), "samples": len(job.sample_ids), "job_dir": str(job.job_dir)}, indent=2), file=sys.stderr, flush=True)
            leaves = job.run()
            summary = []
            for leaf in leaves:
                policy = leaf.metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
                summary.append({"leaf": leaf.leaf_id, "run_id": leaf.run.run_id, "scored": leaf.scored_count, "failed": leaf.failed_count,
                                "eer": policy["eer"]["eer"], "tenfold_mean_accuracy": policy["view2_tenfold"]["mean_accuracy"],
                                "fnmr_at_fmr_1e-3": next((p.get("fnmr") for p in policy["operating_points"] if p["target_fmr"] == 0.001), None)})
            print(json.dumps({"matrix_id": job.matrix_id, "leaves": summary}, indent=2))
            return 0
        if args.command == "utility":
            from .analysis.utility import UtilityAnalysisJob, UtilityRequest
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            request = UtilityRequest(
                matcher_run_id=args.matcher_run_id, quality_job_id=args.quality_job_id, quality_measure=args.quality_measure, pair_quality_policy=args.policy,
                target_fmr=args.target_fmr, matcher_threshold=args.matcher_threshold, quality_threshold=args.quality_threshold, partial_interval=args.partial_interval,
                bootstrap_replicates=args.replicates, seed=args.seed,
            )
            job = UtilityAnalysisJob(store=store, repository_root=settings.repository_root, project=project, actor=service.actor, request=request, job_root=settings.data_dir / "analyses")
            existing = store.utility_analysis(job.analysis_id)
            if existing is not None:
                print(json.dumps({"analysis_id": job.analysis_id, "already_recorded": True, "summary": json.loads(existing["summary_json"])}, indent=2))
                return 0
            result = job.run()
            print(json.dumps({"analysis_id": result.analysis_id, "run_id": result.run.run_id, "directory": str(result.directory), "summary": result.summary}, indent=2))
            return 0
        if args.command == "cross-matcher":
            from .analysis.crossmatcher import CrossMatcherJob, CrossMatcherRequest
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            request = CrossMatcherRequest(matrix_id=args.matrix_id, quality_job_id=args.quality_job_id, quality_measure=args.quality_measure, pair_quality_policy=args.policy, target_fmr=args.target_fmr, partial_interval=args.partial_interval, bootstrap_replicates=args.replicates, seed=args.seed)
            job = CrossMatcherJob(store=store, registry=engine.registry, repository_root=settings.repository_root, project=project, actor=service.actor, request=request, job_root=settings.data_dir / "analyses")
            existing = store.cross_matcher_analysis(job.analysis_id)
            if existing is not None:
                print(json.dumps({"analysis_id": job.analysis_id, "already_recorded": True, "summary": json.loads(existing["summary_json"])}, indent=2))
                return 0
            result = job.run()
            print(json.dumps({"analysis_id": result.analysis_id, "run_id": result.run.run_id, "directory": str(result.directory), "summary": result.summary}, indent=2))
            return 0
        if args.command == "assess-owner":
            from pathlib import Path as _Path

            from .engines.ofiq.batch import OFIQBatchJob
            from .importers.owner_supplied import OwnerSuppliedImporter
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            importer = OwnerSuppliedImporter(store, settings.data_dir, service.actor)
            data = _Path(args.image).read_bytes()
            admitted = importer.admission.admit_bytes(data, _Path(args.image).name, args.project_id)
            dataset, sample = importer.import_admitted_single(project, admitted, f"Uploaded photo {_Path(args.image).name}")
            job = OFIQBatchJob(
                engine=engine, store=store, project=project, dataset=dataset, samples=[sample],
                purpose_scope=args.scope, job_root=settings.data_dir / "jobs", chunks=1,
                selection_rule="the single operator-supplied owner image (decision 30, CLI)",
            )
            operation_request = {
                "interface": "CLI",
                "command": "assess-owner",
                "dataset_id": dataset.dataset_id,
                "sample_id": sample.sample_id,
                "source_sha256": admitted.sha256,
                "purpose_scope": args.scope,
                "execution": "host",
                "sample_count": 1,
                "selection_rule": job.selection_rule,
            }
            progress = _run_quality_batch_operation(store, job, operation_request)
            print(json.dumps({"run_id": progress.run_id, "state": progress.state, "source_kind": "OWNER_SUPPLIED_LOCAL_INPUT", "image_sha256": admitted.sha256, "stored_path": admitted.path, "dataset_id": dataset.dataset_id}, indent=2))
            return 0 if progress.state == "COMPLETED" else 5
        if args.command == "import-owner-directory":
            from pathlib import Path as _Path

            from .importers.owner_supplied import OwnerSuppliedImporter
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            result = OwnerSuppliedImporter(store, settings.data_dir, service.actor).import_directory(project, _Path(args.root), args.name)
            print(json.dumps({"dataset_id": result.dataset.dataset_id, "samples": result.sample_count, "subjects": result.subject_count, "source_kind": "OWNER_SUPPLIED_LOCAL_INPUT", "statistics_sha256": result.statistics_sha256}, indent=2))
            return 0
        if args.command == "freeze-owner-pairs":
            from .importers.owner_supplied import (
                OWNER_PAIR_MAX_OUTPUT_PAIRS,
                freeze_owner_pairs,
            )
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            dataset = store.dataset(args.dataset_id)
            if dataset is None:
                raise SystemExit("dataset is not recorded")
            record = freeze_owner_pairs(store, project, dataset, data_dir=settings.data_dir, actor_id=service.actor, seed=args.seed, max_pairs=OWNER_PAIR_MAX_OUTPUT_PAIRS, max_mated_pairs_per_subject=args.max_mated_per_subject, nonmated_multiple=args.nonmated_multiple)
            print(json.dumps({"protocol_id": record.protocol_id, "pairs": record.pair_count, "mated": record.mated_count, "nonmated": record.nonmated_count, "pairs_sha256": record.pairs_sha256}, indent=2))
            return 0
        if args.command == "identification":
            from .identification.protocol import freeze_lfw_identification
            from .identification.search import IdentificationSearchJob
            from .matchers.upstream import UpstreamSources
            from .matchers.runtime import MatcherId, UPSTREAM_KEYS
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            dataset = store.dataset(args.dataset_id)
            if dataset is None:
                raise SystemExit("dataset is not recorded")
            protocol = freeze_lfw_identification(store, project=project, dataset=dataset, selection=args.selection, rank_depth=args.rank_depth, repository_root=settings.repository_root, actor=service.actor)
            print(json.dumps({"protocol_id": protocol["protocol_id"], "gallery": protocol["gallery_count"], "mated_probes": protocol["mated_probe_count"], "nonmated_probes": protocol["nonmated_probe_count"]}, indent=2), file=sys.stderr, flush=True)
            upstream = UpstreamSources(settings.repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json", settings.data_dir, cache_root=settings.upstream_cache_root)
            try:
                for key in UPSTREAM_KEYS[MatcherId(args.matcher_id)]:
                    upstream.verify(key)
            except Exception as error:  # noqa: BLE001 - actionable dependency refusal at the CLI boundary
                raise SystemExit(f"matcher architecture sources are not ready; run `qualsight setup` explicitly: {error}") from error
            job = IdentificationSearchJob(store=store, registry=engine.registry, upstream=upstream, repository_root=settings.repository_root, project=project, protocol=protocol, entries=store.identification_entries(protocol["protocol_id"]), matcher_id=args.matcher_id, condition_id=args.condition_id, job_root=settings.data_dir / "identification", actor=service.actor, execution_timeout_seconds=settings.native_execution_timeout_seconds, quality_job_id=args.quality_job_id)
            result = job.run()
            print(json.dumps({"search_id": result.search_id, "run_id": result.run.run_id, "protocol_id": protocol["protocol_id"], "summary": result.summary, "gallery_effects": result.metrics["gallery_effects"]["gallery_sizes"], "operating_points": result.metrics["open_set"]["EXCLUDE_FAILED_TRIALS"]["operating_points"], "rank_k": {k: v["rate"] for k, v in result.metrics["closed_set"]["EXCLUDE_FAILED_TRIALS"]["rank_k"].items()}}, indent=2))
            return 0
        if args.command == "import-external-scores":
            from .importers.external_scores import ExternalScoreImporter
            from .projects import ProjectService

            engine = _engine(settings)
            service = ProjectService(store, engine)
            project = service.require_writable(args.project_id)
            protocol = store.protocol(args.protocol_id)
            if protocol is None:
                raise SystemExit("protocol is not recorded")
            importer = ExternalScoreImporter(store=store, registry=engine.registry, repository_root=settings.repository_root, project=project, protocol=protocol, resource_id=args.resource_id, job_root=settings.data_dir / "external", actor=service.actor)
            result = _run_external_score_operation(store, importer)
            policy = result.metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
            print(json.dumps({"leaf": result.leaf_id, "run_id": result.run.run_id, "provenance_marker": "EXTERNALLY_COMPUTED", "scored": result.scored_count, "failed": result.failed_count, "eer": policy["eer"]["eer"], "fnmr_at_fmr_1e-3": next((p.get("fnmr") for p in policy["operating_points"] if p["target_fmr"] == 0.001), None), "mapping": result.mapping}, indent=2))
            return 0
        if args.command == "setup":
            from .matchers.upstream import UpstreamSources

            upstream = UpstreamSources(settings.repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json", settings.data_dir, cache_root=settings.upstream_cache_root)
            with permit_explicit_setup_network():
                acquired = upstream.acquire()
            summary = {"upstream_sources": {key: str(path) for key, path in acquired.items()}, "data_dir": str(settings.data_dir), "registry": str(settings.registry_path)}
            if args.verify_registry:
                import subprocess

                result = subprocess.run([sys.executable, str(settings.repository_root / "scripts" / "validate_resource_registry.py"), str(settings.registry_path)], capture_output=True, text=True, check=False)
                summary["registry_validation"] = {"exit_code": result.returncode, "output": result.stdout.strip()[-400:] + result.stderr.strip()[-400:]}
            print(json.dumps(summary, indent=2))
            return 0
        if args.command == "bundle-export":
            from .reporting.bundle import export_bundle

            record = export_bundle(
                store,
                run_id=args.run_id,
                root=settings.data_dir / "bundles",
                repository_root=settings.repository_root,
                record_publication=False,
            )
            store.record_reporting_publication(
                "EXPERIMENT_BUNDLE",
                record,
                reporting_publication_audit_event(
                    "EXPERIMENT_BUNDLE", record, actor_id=settings.actor
                ),
            )
            print(json.dumps({k: v for k, v in record.items() if k != "manifest"}, indent=2))
            return 0
        if args.command == "reproduce-bundle":
            from dataclasses import asdict

            from .projects import ProjectService
            from .reporting.bundle import reproduce_bundle

            engine = _engine(settings)
            result = reproduce_bundle(args.bundle, store=store, registry=engine.registry, repository_root=settings.repository_root, work_root=settings.data_dir / "reproductions", project_service=ProjectService(store, engine), execution_timeout_seconds=settings.native_execution_timeout_seconds, engine=engine)
            print(json.dumps(asdict(result), indent=2, default=str))
            return 0 if result.state in {"EXACT", "WITHIN_TOLERANCE"} else 3
        if args.command == "figure":
            from .reporting import figures as figure_module

            spec = figure_module.build(store, args.project_id, args.kind, json.loads(args.params))
            record = figure_module.render(spec, settings.data_dir / "figures")
            store.record_reporting_publication(
                "FIGURE",
                record,
                reporting_publication_audit_event(
                    "FIGURE", record, actor_id=settings.actor
                ),
            )
            print(json.dumps({"figure_id": record["figure_id"], "files": record["files"], "source_run_ids": record["source_run_ids"]}, indent=2))
            return 0
        if args.command == "export-run":
            manifest = export_run_manifest(store, args.run_id, args.out)
            print(json.dumps({"path": str(args.out), "manifest_sha256": manifest["manifest_sha256"], "run_id": args.run_id}, indent=2))
            return 0
    finally:
        try:
            store.close()
        finally:
            lease.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
