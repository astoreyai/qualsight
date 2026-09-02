"""Validate path privacy for structured evidence from a real batch warning.

The gate copies two hash-pinned LFW files into an isolated operator-owned tree,
admits that tree through the real owner-directory importer, then temporarily
withholds one copied file.  The real batch engine records the resulting
integrity failure as a path-free sample/outcome/reason object.  Chromium must
render a readable outcome while the project-scoped public Run projection remains
an explicit, collapsed secondary action. A source assertion retains the UI's defensive
sanitizer for pre-migration Runs that contain legacy path-bearing warnings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import traceback
import urllib.parse

from controlled_inputs import lfw_tree_root
from validate_comparison_studio import _json_request, _port_is_free, _stop, _wait_http


ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5201
SOURCE_PORT = 5202
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
FIXTURE_MANIFEST = ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json"
EXPECTED_FIXTURE_SHA256 = "6d35c169199d83c12e0c76c90a0b1e67c432d0cf78df3aea66df293f65b8b6fe"
LFW_ROOT = lfw_tree_root(ROOT)
REAL_INPUTS = {
    "Abel_Pacheco/Abel_Pacheco_0001.jpg": "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967",
    "Abel_Pacheco/Abel_Pacheco_0004.jpg": "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae",
}
SAFE_LEGACY_WARNING = "A recorded sample was not assessed because its source bytes were unreadable."
LEGACY_PRODUCER_COMMIT = "fb159edc0a069151871b8bcba2fb460352a50e32"
LEGACY_BATCH_SOURCE_SHA256 = "58190cad205ed608fe050bb38608df73d34d69ac516143191fa6426e30e9884e"


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable


def _assert_frontend_contract() -> None:
    source = (ROOT / "frontend" / "src" / "EvidenceDetail.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    home_source = (ROOT / "frontend" / "src" / "Home.tsx").read_text(encoding="utf-8")
    utility_source = (ROOT / "frontend" / "src" / "Utility.tsx").read_text(encoding="utf-8")
    required = {
        "safeRunWarning": "the defensive legacy warning sanitizer",
        "containsAbsoluteFilesystemPath": "legacy absolute-path detection",
        "private local evidence": "the private legacy evidence boundary",
        "View public Run JSON": "the public Run projection view action",
        "Download public Run JSON": "the public Run projection download action",
        'Object.keys(parsed)': "closed structured-warning field validation",
        "file:": "file-URI path detection",
        "unc:": "UNC path detection",
        "tilde:": "tilde-home path detection",
        "An unstructured recorded warning could not be safely presented.": (
            "fail-closed unstructured warning rendering"
        ),
    }
    missing = [description for marker, description in required.items() if marker not in source]
    if missing:
        raise AssertionError(f"EvidenceDetail warning privacy contract is missing: {missing!r}")
    if "View raw JSON" in source or "Download raw JSON" in source:
        raise AssertionError("EvidenceDetail still mislabels the public Run projection as raw evidence")
    if "quality_result_available: boolean" not in api_source:
        raise AssertionError("JobOut does not expose persisted quality-result availability")
    if "job.quality_result_available" not in home_source:
        raise AssertionError("Home may request quality measures for a job without a persisted result")
    if "job.state === \"COMPLETED\" && job.run_id && job.quality_result_available" not in utility_source:
        raise AssertionError("Utility may request quality measures for a job without a persisted result")
    if "jobsProjectId" not in utility_source or "jobsProjectId !== projectId" not in utility_source:
        raise AssertionError("Utility quality-measure requests are not bound to their project job feed")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _environment(data_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }


def _start_api(data_dir: Path, source_root: Path | None = None) -> subprocess.Popen[bytes]:
    if source_root is not None:
        command = [
            str(ROOT / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "qualsight.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
            "--log-level",
            "warning",
        ]
    else:
        command = [
            _uv_binary(),
            "run",
            "--frozen",
            "--offline",
            "--directory",
            "backend",
            "python",
            "-m",
            "uvicorn",
            "qualsight.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
            "--log-level",
            "warning",
        ]
    return subprocess.Popen(
        command,
        cwd=ROOT / "backend" if source_root is not None else ROOT,
        env={
            **_environment(data_dir),
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            "PYTHONPATH": f"{(source_root or ROOT) / 'backend' / 'src'}:{ROOT}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _start_preview(data_dir: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "pnpm",
            "--filter",
            "@qualsight/frontend",
            "exec",
            "vite",
            "preview",
            "--host",
            "127.0.0.1",
            "--port",
            str(PREVIEW_PORT),
            "--strictPort",
        ],
        cwd=ROOT,
        env=_environment(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _start_source_server(data_dir: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "pnpm",
            "--filter",
            "@qualsight/frontend",
            "exec",
            "vite",
            "--host",
            "127.0.0.1",
            "--port",
            str(SOURCE_PORT),
            "--strictPort",
        ],
        cwd=ROOT,
        env=_environment(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_job(project_id: str, job_id: str) -> dict:
    encoded_project = urllib.parse.quote(project_id, safe="")
    encoded_job = urllib.parse.quote(job_id, safe="")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/jobs/{encoded_job}"
        )
        if not isinstance(result, dict):
            raise AssertionError("quality job status is not a JSON object")
        if result.get("state") in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return result
        time.sleep(0.25)
    raise TimeoutError(f"quality job {job_id} did not reach a terminal state")


def _wait_legacy_job(project_id: str, job_id: str) -> dict:
    encoded_job = urllib.parse.quote(job_id, safe="")
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = _json_request(f"http://127.0.0.1:{API_PORT}/api/jobs/{encoded_job}")
        if not isinstance(result, dict):
            raise AssertionError("legacy quality job status is not a JSON object")
        if result.get("state") in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return result
        time.sleep(0.25)
    raise TimeoutError(f"legacy quality job {job_id} did not reach a terminal state")


def _extract_legacy_producer(target: Path) -> dict[str, str]:
    archive = target.parent / f"{target.name}.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive), LEGACY_PRODUCER_COMMIT],
        cwd=ROOT,
        check=True,
    )
    target.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode="r") as bundle:
            for member in bundle.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isreg()):
                    raise RuntimeError(f"legacy producer archive contains an unsafe member: {member.name}")
                destination = target / path
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise RuntimeError(f"legacy producer archive member is unreadable: {member.name}")
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
                destination.chmod(member.mode & 0o777)
    finally:
        archive.unlink(missing_ok=True)
    source = target / "backend" / "src" / "qualsight" / "engines" / "ofiq" / "batch.py"
    source_sha256 = _sha256(source)
    if source_sha256 != LEGACY_BATCH_SOURCE_SHA256:
        raise RuntimeError("the pinned legacy batch producer source hash drifted")
    source_text = source.read_text(encoding="utf-8")
    legacy_expression = 'warnings=tuple(f"{row.source_path}: {row.failure_reason}"'
    if legacy_expression not in source_text:
        raise RuntimeError("the pinned source no longer proves the legacy path-bearing warning producer")
    return {"commit": LEGACY_PRODUCER_COMMIT, "batch_source_sha256": source_sha256}


def _verified_real_inputs(operator_root: Path) -> dict[str, Path]:
    if _sha256(FIXTURE_MANIFEST) != EXPECTED_FIXTURE_SHA256:
        raise RuntimeError("the immutable LFW subset manifest hash drifted")
    fixture = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    registered = {
        str(item["path"]): str(item["sha256"])
        for item in fixture["images"]
        if isinstance(item, dict) and "path" in item and "sha256" in item
    }
    copied: dict[str, Path] = {}
    for relative, expected_hash in REAL_INPUTS.items():
        if registered.get(relative) != expected_hash:
            raise RuntimeError(f"the LFW fixture does not bind {relative} to its expected hash")
        source = LFW_ROOT / relative
        if not source.is_file() or _sha256(source) != expected_hash:
            raise RuntimeError(f"the registered real LFW input is unavailable or drifted: {relative}")
        target = operator_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _sha256(target) != expected_hash:
            raise RuntimeError(f"the isolated real LFW copy drifted: {relative}")
        copied[relative] = target
    return copied


def _produce_legacy_warning_run(
    data_dir: Path,
    legacy_root: Path,
    operator_root: Path,
    admitted_path: Path,
    withheld_path: Path,
) -> dict[str, str]:
    api_process = _start_api(data_dir, legacy_root)
    try:
        try:
            _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        except RuntimeError as error:
            _stop(api_process)
            output = api_process.stdout.read().decode("utf-8", errors="replace") if api_process.stdout else ""
            raise RuntimeError(f"pinned legacy producer did not start: {output[-2000:]}") from error
        project = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Pinned beta-2 legacy warning producer",
                "description": "Real hash-pinned LFW bytes processed by the recorded beta-2 batch producer.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        if not isinstance(project, dict):
            raise AssertionError("legacy project creation did not return a record")
        project_id = str(project["project_id"])
        encoded_project = urllib.parse.quote(project_id, safe="")
        _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/open",
            method="POST",
        )
        dataset = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/datasets/import-owner-directory",
            method="POST",
            body={"root": str(operator_root), "name": "Pinned real LFW legacy-warning input"},
        )
        if not isinstance(dataset, dict) or dataset.get("sample_count") != 2:
            raise AssertionError("legacy producer did not admit both real LFW files")
        dataset_id = str(dataset["dataset_id"])
        encoded_dataset = urllib.parse.quote(dataset_id, safe="")
        admitted_path.rename(withheld_path)
        try:
            started = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/datasets/{encoded_dataset}/quality-batch",
                method="POST",
                body={
                    "selection": {"kind": "all", "limit": 2},
                    "chunks": 1,
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            if not isinstance(started, dict):
                raise AssertionError("legacy quality-batch admission did not return a job")
            job = _wait_legacy_job(project_id, str(started["job_id"]))
        finally:
            withheld_path.rename(admitted_path)
        if job.get("state") != "COMPLETED" or not job.get("run_id"):
            raise AssertionError(f"legacy mixed-integrity batch did not complete with a Run: {job!r}")
        run_id = str(job["run_id"])
        with sqlite3.connect(
            f"file:{(data_dir / 'qualsight.sqlite3').absolute()}?mode=ro",
            uri=True,
        ) as connection:
            row = connection.execute(
                "SELECT record_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AssertionError("pinned legacy producer private Run record is absent")
        private_record = json.loads(str(row[0]))
        private_warnings = private_record.get("warnings")
        if not isinstance(private_warnings, list) or len(private_warnings) != 1:
            raise AssertionError(
                f"pinned legacy producer did not privately record one warning: {private_warnings!r}"
            )
        warning = str(private_warnings[0])
        if str(admitted_path) not in warning or "unreadable" not in warning.lower():
            raise AssertionError("pinned legacy producer warning lacks the real path-bearing failure")
        return {
            "project_id": project_id,
            "dataset_id": dataset_id,
            "job_id": str(job["job_id"]),
            "run_id": run_id,
            "warning": warning,
            "warning_sha256": hashlib.sha256(warning.encode("utf-8")).hexdigest(),
        }
    finally:
        _stop(api_process)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()

    _assert_frontend_contract()

    if not all(_port_is_free(port) for port in (API_PORT, PREVIEW_PORT, SOURCE_PORT)):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT}/{SOURCE_PORT} must be free")
    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-evidence-warning-"))
    legacy_parent = Path(tempfile.mkdtemp(prefix="qualsight-legacy-warning-producer-"))
    legacy_root = legacy_parent / "source"
    operator_root = data_dir / "operator-inputs"
    copied = _verified_real_inputs(operator_root)
    admitted_path = copied["Abel_Pacheco/Abel_Pacheco_0004.jpg"]
    withheld_path = admitted_path.with_suffix(".withheld")
    api_process: subprocess.Popen[bytes] | None = None
    preview_process: subprocess.Popen[bytes] | None = None
    source_process: subprocess.Popen[bytes] | None = None
    observed: dict[str, object] = {
        "kind": "QUALSIGHT_EVIDENCE_WARNING_PRIVACY_VALIDATION",
        "fixture_manifest_sha256": EXPECTED_FIXTURE_SHA256,
        "real_input_sha256": sorted(REAL_INPUTS.values()),
        "passed": False,
    }
    exit_code = 1
    try:
        legacy_provenance = _extract_legacy_producer(legacy_root)
        legacy_run = _produce_legacy_warning_run(
            data_dir,
            legacy_root,
            operator_root,
            admitted_path,
            withheld_path,
        )
        api_process = _start_api(data_dir)
        preview_process = _start_preview(data_dir)
        source_process = _start_source_server(data_dir)
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)
        _wait_http(f"http://127.0.0.1:{SOURCE_PORT}/", 60)

        legacy_envelope = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/"
            f"{urllib.parse.quote(legacy_run['project_id'], safe='')}/runs/"
            f"{urllib.parse.quote(legacy_run['run_id'], safe='')}"
        )
        public_warnings = (
            legacy_envelope.get("run", {}).get("warnings")
            if isinstance(legacy_envelope, dict)
            else None
        )
        if not isinstance(public_warnings, list) or len(public_warnings) != 1:
            raise AssertionError(
                "current public projection omitted the pinned beta-2 warning: "
                f"{public_warnings!r}"
            )
        if str(admitted_path) in str(public_warnings[0]):
            raise AssertionError("current public projection exposed the beta-2 source path")
        legacy_run["public_warning"] = str(public_warnings[0])

        project = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Evidence warning privacy real browser gate",
                "description": "A real owner dataset with one naturally unavailable admitted file.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        if not isinstance(project, dict):
            raise AssertionError("project creation did not return a record")
        project_id = str(project["project_id"])
        encoded_project = urllib.parse.quote(project_id, safe="")
        _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/open",
            method="POST",
        )
        dataset = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/datasets/import-owner-directory",
            method="POST",
            body={"root": str(operator_root), "name": "Hash-pinned real LFW warning fixture"},
        )
        if not isinstance(dataset, dict) or dataset.get("sample_count") != 2:
            raise AssertionError("real owner-directory import did not record both LFW files")
        dataset_id = str(dataset["dataset_id"])
        encoded_dataset = urllib.parse.quote(dataset_id, safe="")
        samples = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/datasets/{encoded_dataset}/samples"
        )
        if not isinstance(samples, list) or len(samples) != 2:
            raise AssertionError("real owner dataset does not expose both recorded sample identities")
        sample_ids = {
            str(sample["sample_id"])
            for sample in samples
            if isinstance(sample, dict) and isinstance(sample.get("sample_id"), str)
        }
        if len(sample_ids) != 2:
            raise AssertionError("real owner dataset sample identities are incomplete")

        admitted_path.rename(withheld_path)
        try:
            started = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/datasets/{encoded_dataset}/quality-batch",
                method="POST",
                body={
                    "selection": {"kind": "all", "limit": 2},
                    "chunks": 1,
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            if not isinstance(started, dict):
                raise AssertionError("quality-batch admission did not return a job")
            job = _wait_job(project_id, str(started["job_id"]))
        finally:
            withheld_path.rename(admitted_path)

        if job.get("state") != "COMPLETED" or not job.get("run_id"):
            raise AssertionError(f"real mixed-integrity batch did not complete with a Run: {job!r}")
        run_id = str(job["run_id"])
        encoded_run = urllib.parse.quote(run_id, safe="")
        run_href = f"/api/projects/{encoded_project}/runs/{encoded_run}"
        projected_job_contract: dict[str, list[dict[str, object]]] = {}
        for recorded_project_id in (project_id, str(legacy_run["project_id"])):
            encoded_recorded_project = urllib.parse.quote(recorded_project_id, safe="")
            projected_jobs = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_recorded_project}/jobs"
            )
            if not isinstance(projected_jobs, list):
                raise AssertionError("project job feed is not a JSON list")
            projected_job_contract[recorded_project_id] = []
            for projected_job in projected_jobs:
                if not isinstance(projected_job, dict):
                    raise AssertionError("project job feed contains a non-object item")
                availability = projected_job.get("quality_result_available")
                if not isinstance(availability, bool):
                    raise AssertionError("project job omitted persisted quality-result availability")
                projected_job_contract[recorded_project_id].append(
                    {
                        "job_id": str(projected_job.get("job_id")),
                        "quality_result_available": availability,
                    }
                )
                if availability:
                    encoded_projected_job = urllib.parse.quote(str(projected_job["job_id"]), safe="")
                    _json_request(
                        f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_recorded_project}/jobs/"
                        f"{encoded_projected_job}/quality-measures"
                    )
        run_envelope = _json_request(f"http://127.0.0.1:{API_PORT}{run_href}")
        if not isinstance(run_envelope, dict) or not isinstance(run_envelope.get("run"), dict):
            raise AssertionError("recorded batch Run response has no Run envelope")
        warnings = run_envelope["run"].get("warnings")
        if not isinstance(warnings, list) or len(warnings) != 1:
            raise AssertionError(f"real mixed-integrity batch did not record one structured warning: {warnings!r}")
        recorded_warning = str(warnings[0])
        try:
            structured_warning = json.loads(recorded_warning)
        except json.JSONDecodeError as error:
            raise AssertionError("the real batch warning is not structured JSON") from error
        if not isinstance(structured_warning, dict) or set(structured_warning) != {"sample_id", "outcome", "reason"}:
            raise AssertionError(f"the real batch warning fields differ: {structured_warning!r}")
        if structured_warning["sample_id"] not in sample_ids:
            raise AssertionError("the real batch warning is not bound to an admitted sample")
        if structured_warning["outcome"] != "INTEGRITY_FAILURE":
            raise AssertionError(f"the real batch warning outcome differs: {structured_warning['outcome']!r}")
        if not isinstance(structured_warning["reason"], str) or "unreadable" not in structured_warning["reason"].lower():
            raise AssertionError(f"the real batch warning reason differs: {structured_warning['reason']!r}")
        if "source_path" in recorded_warning or str(admitted_path) in recorded_warning or str(data_dir) in recorded_warning:
            raise AssertionError("the newly recorded structured warning contains a filesystem path")
        provenance_limits = run_envelope["run"].get("provenance_limits")
        if (
            not isinstance(provenance_limits, list)
            or not provenance_limits
            or not isinstance(provenance_limits[0], str)
        ):
            raise AssertionError("the real batch Run has no recorded unstructured claim boundary")
        real_unstructured_warning = provenance_limits[0]

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            console_errors: list[dict[str, object]] = []
            page.on(
                "console",
                lambda message: console_errors.append(
                    {"text": message.text, "location": message.location}
                )
                if message.type == "error"
                else None,
            )
            page.add_init_script(
                "window.localStorage.setItem('qualsight.activeProjectId', "
                f"{json.dumps(project_id)})"
            )
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/projects", wait_until="networkidle")
            action = page.locator(
                f"[data-testid=evidence-open][data-evidence-href='{run_href}']"
            )
            action.wait_for(state="visible", timeout=60_000)
            action.focus()
            page.keyboard.press("Enter")
            detail = page.locator(
                f"[data-testid=evidence-detail][data-evidence-id='{run_id}']"
                f"[data-project-id='{project_id}']"
            )
            detail.wait_for(state="visible", timeout=60_000)
            primary_text = detail.inner_text()
            if str(admitted_path) in primary_text or str(data_dir) in primary_text:
                raise AssertionError(
                    "primary structured evidence exposed the absolute recorded owner-source path"
                )
            if SAFE_LEGACY_WARNING not in primary_text:
                raise AssertionError(
                    "primary structured evidence did not preserve the safe sample outcome and reason"
                )
            if recorded_warning in primary_text or '"sample_id"' in primary_text or '"outcome"' in primary_text:
                raise AssertionError("primary structured evidence rendered the literal structured warning payload")
            actions = detail.locator("details[data-testid=evidence-actions]")
            if actions.count() != 1 or actions.evaluate("element => element.open"):
                raise AssertionError("public Run JSON is not a single collapsed secondary disclosure")
            raw_view = actions.locator("[data-testid=evidence-raw-view]")
            raw_download = actions.locator("[data-testid=evidence-raw-download]")
            if raw_view.count() != 1 or raw_download.count() != 1:
                raise AssertionError("secondary public Run JSON view/download actions are incomplete")
            public_action_labels = (
                (raw_view.text_content() or "").strip(),
                (raw_download.text_content() or "").strip(),
            )
            if public_action_labels != ("View public Run JSON", "Download public Run JSON"):
                raise AssertionError(
                    "project-scoped Run actions are mislabeled as raw private evidence: "
                    f"{public_action_labels!r}"
                )
            raw_href = raw_view.get_attribute("href")
            if raw_href != run_href or raw_download.get_attribute("href") != raw_href:
                raise AssertionError("secondary public Run actions do not retain the recorded Run identity")
            raw_response = page.request.get(f"http://127.0.0.1:{PREVIEW_PORT}{raw_href}")
            if not raw_response.ok:
                raise AssertionError("secondary public Run response is unavailable")
            raw_envelope = raw_response.json()
            raw_warnings = raw_envelope.get("run", {}).get("warnings") if isinstance(raw_envelope, dict) else None
            if raw_warnings != [recorded_warning]:
                raise AssertionError("secondary public Run response changed the recorded structured warning")
            raw_warning_text = str(raw_warnings[0])
            if "source_path" in raw_warning_text or str(admitted_path) in raw_warning_text or str(data_dir) in raw_warning_text:
                raise AssertionError("secondary public Run warning exposed a filesystem path")

            detail.get_by_role("button", name="Close").click()
            detail.wait_for(state="detached", timeout=30_000)
            legacy_project_id = legacy_run["project_id"]
            legacy_run_id = legacy_run["run_id"]
            legacy_href = (
                f"/api/projects/{urllib.parse.quote(legacy_project_id, safe='')}/runs/"
                f"{urllib.parse.quote(legacy_run_id, safe='')}"
            )
            page.locator("[data-testid=project-select]").select_option(legacy_project_id)
            legacy_action = page.locator(
                f"[data-testid=evidence-open][data-evidence-href='{legacy_href}']"
            )
            legacy_action.wait_for(state="visible", timeout=60_000)
            legacy_action.focus()
            page.keyboard.press("Enter")
            legacy_detail = page.locator(
                f"[data-testid=evidence-detail][data-evidence-id='{legacy_run_id}']"
                f"[data-project-id='{legacy_project_id}']"
            )
            legacy_detail.wait_for(state="visible", timeout=60_000)
            legacy_primary_text = legacy_detail.inner_text()
            if str(admitted_path) in legacy_primary_text or str(data_dir) in legacy_primary_text:
                raise AssertionError("primary Evidence exposed the real beta-2 path-bearing warning")
            if SAFE_LEGACY_WARNING not in legacy_primary_text:
                raise AssertionError(
                    "primary Evidence did not safely summarize the real beta-2 warning: "
                    f"public={legacy_run['public_warning']!r}, rendered={legacy_primary_text!r}"
                )
            legacy_actions = legacy_detail.locator("details[data-testid=evidence-actions]")
            if legacy_actions.count() != 1 or legacy_actions.evaluate("element => element.open"):
                raise AssertionError("legacy public Run JSON is not a collapsed secondary disclosure")
            legacy_raw_response = page.request.get(
                f"http://127.0.0.1:{PREVIEW_PORT}{legacy_href}"
            )
            if not legacy_raw_response.ok:
                raise AssertionError("recorded beta-2 public Run response is unavailable")
            legacy_raw_envelope = legacy_raw_response.json()
            legacy_raw_warnings = (
                legacy_raw_envelope.get("run", {}).get("warnings")
                if isinstance(legacy_raw_envelope, dict)
                else None
            )
            if legacy_raw_warnings != [legacy_run["public_warning"]]:
                raise AssertionError("secondary public Run projection changed its redacted beta-2 warning")
            if str(admitted_path) in str(legacy_raw_warnings[0]) or str(data_dir) in str(legacy_raw_warnings[0]):
                raise AssertionError("secondary public Run projection exposed beta-2 path provenance")

            source_page = browser.new_page()
            source_page.goto(f"http://127.0.0.1:{SOURCE_PORT}/", wait_until="networkidle")
            home_uv = Path.home() / ".local" / "bin" / "uv"
            if not home_uv.is_file():
                raise RuntimeError("the recorded workstation uv path used for tilde-form validation is absent")
            real_parts = admitted_path.parts[1:]
            path_forms = {
                "posix": str(admitted_path),
                "embedded_posix": f"failure:{admitted_path}",
                "windows": "C:\\" + "\\".join(real_parts),
                "unc": "\\\\localhost\\" + "\\".join(real_parts),
                "file": admitted_path.as_uri(),
                "tilde": "~/.local/bin/uv",
            }
            formatter_evidence = source_page.evaluate(
                """async ({ forms, legacyWarning, publicWarning, unstructuredWarning }) => {
                  const module = await import('/src/EvidenceDetail.tsx');
                  const formsResult = Object.fromEntries(Object.entries(forms).map(([kind, path]) => {
                    const warning = `No such file or directory: ${path}`;
                    return [kind, {
                      detected: module.containsAbsoluteFilesystemPath(warning),
                      rendered: module.safeRunWarning(warning),
                    }];
                  }));
                  return {
                    forms: formsResult,
                    recordedLegacy: {
                      detected: module.containsAbsoluteFilesystemPath(legacyWarning),
                      rendered: module.safeRunWarning(legacyWarning),
                    },
                    publicLegacy: {
                      source: publicWarning,
                      rendered: module.safeRunWarning(publicWarning),
                    },
                    unstructuredBoundary: module.safeRunWarning(unstructuredWarning),
                  };
                }""",
                {
                    "forms": path_forms,
                    "legacyWarning": legacy_run["warning"],
                    "publicWarning": legacy_run["public_warning"],
                    "unstructuredWarning": real_unstructured_warning,
                },
            )
            for path_kind, result in formatter_evidence["forms"].items():
                if result != {"detected": True, "rendered": SAFE_LEGACY_WARNING}:
                    raise AssertionError(
                        f"warning formatter did not fail closed for {path_kind}: {result!r}"
                    )
            if formatter_evidence["recordedLegacy"] != {
                "detected": True,
                "rendered": SAFE_LEGACY_WARNING,
            }:
                raise AssertionError(
                    "warning formatter did not fail closed for the real privately recorded legacy warning: "
                    f"{formatter_evidence['recordedLegacy']!r}"
                )
            if formatter_evidence["publicLegacy"]["rendered"] != SAFE_LEGACY_WARNING:
                raise AssertionError(
                    "warning formatter did not preserve the real path-free legacy warning: "
                    f"{formatter_evidence['publicLegacy']!r}"
                )
            if formatter_evidence["unstructuredBoundary"] != (
                "An unstructured recorded warning could not be safely presented. "
                "Its full detail remains in private local evidence."
            ):
                raise AssertionError(
                    "unknown unstructured warning text was rendered instead of failing closed: "
                    f"{formatter_evidence['unstructuredBoundary']!r}"
                )
            source_page.close()
            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)
            if console_errors:
                raise AssertionError(f"browser console errors: {console_errors!r}")
            browser.close()

        observed["contract"] = {
            "project_id": project_id,
            "run_id": run_id,
            "job_id": job["job_id"],
            "structured_warning": structured_warning,
            "recorded_warning_sha256": hashlib.sha256(recorded_warning.encode("utf-8")).hexdigest(),
            "unstructured_warning_sha256": hashlib.sha256(
                real_unstructured_warning.encode("utf-8")
            ).hexdigest(),
            "recorded_path_sha256": hashlib.sha256(str(admitted_path).encode("utf-8")).hexdigest(),
            "primary_path_hidden": True,
            "safe_warning": SAFE_LEGACY_WARNING,
            "public_run_actions_collapsed": True,
            "public_structured_warning_preserved": True,
            "public_run_warning_path_free": True,
            "legacy_producer": legacy_provenance,
            "legacy_run_id": legacy_run["run_id"],
            "legacy_warning_sha256": legacy_run["warning_sha256"],
            "legacy_quality_catalog_compatibility_errors": 0,
            "projected_job_contract": projected_job_contract,
            "path_forms_fail_closed": sorted(path_forms),
        }
        observed["passed"] = True
        exit_code = 0
    except Exception as error:
        observed["failure"] = {
            "type": type(error).__name__,
            "detail": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        if preview_process is not None:
            _stop(preview_process)
        if source_process is not None:
            _stop(source_process)
        if api_process is not None:
            _stop(api_process)
        observed["api_exit_code"] = api_process.returncode if api_process is not None else None
        observed["preview_exit_code"] = preview_process.returncode if preview_process is not None else None
        observed["source_exit_code"] = source_process.returncode if source_process is not None else None
        args.observation.write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(legacy_parent, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
