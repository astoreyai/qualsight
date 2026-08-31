"""Offline operation gate (PRD §§46–48; QS-H72).

Starts the installed product exactly as a researcher would (``python -m
qualsight.cli serve`` — console + API on 127.0.0.1:8480) with the runtime
network guard enabled (``QUALSIGHT_NETWORK_GUARD=1``), then runs the canonical
workflow through the API against the installed data directory: health, console
page, project, LFW import, single-image assessment, fixture-pair quality batch,
protocol freeze, external score import (QS-C21), fixture-pair matcher matrix,
utility analysis, cross-matcher contrasts and disagreement drilldown, figure,
dashboard save/reload, report, bundle, reproduction, purge-scope listing, audit. Every non-loopback connection the
runtime attempts is refused by the guard and counted; the gate passes only when
every step succeeds and the guard counted zero attempts.

Usage: python3 scripts/offline_gate.py --observation atg/evidence/<file>.json [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API_HOST = "127.0.0.1"
API_PORT = 8480
API = f"http://{API_HOST}:{API_PORT}"
UV = "/home/aaron/.local/bin/uv"
BETA_REQUIRED_LEAVES = (
    "QS-C22.AS",
    "QS-C22.AN",
    "QS-C22.ES",
    "QS-C22.EN",
    "QS-C22.TS",
    "QS-C22.TN",
)


def _assert_endpoint_available(host: str, port: int) -> None:
    """Fail before launch when another process already owns the product endpoint."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as error:
            raise RuntimeError(
                f"{host}:{port} already has a listener; offline gate refused"
            ) from error


def _listener_inodes(host: str, port: int) -> set[str]:
    encoded_host = socket.inet_aton(host)[::-1].hex().upper()
    encoded_endpoint = f"{encoded_host}:{port:04X}"
    inodes: set[str] = set()
    try:
        lines = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return inodes
    for line in lines:
        fields = line.split()
        if len(fields) >= 10 and fields[1] == encoded_endpoint and fields[3] == "0A":
            inodes.add(fields[9])
    return inodes


def _process_group_socket_inodes(process_group: int) -> set[str]:
    inodes: set[str] = set()
    for process_directory in Path("/proc").iterdir():
        if not process_directory.name.isdigit():
            continue
        process_id = int(process_directory.name)
        try:
            if os.getpgid(process_id) != process_group:
                continue
            descriptors = process_directory / "fd"
            for descriptor in descriptors.iterdir():
                target = os.readlink(descriptor)
                if target.startswith("socket:[") and target.endswith("]"):
                    inodes.add(target[8:-1])
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return inodes


def _wait_for_owned_listener(
    server: subprocess.Popen[bytes],
    host: str,
    port: int,
    *,
    timeout: float,
) -> None:
    """Wait until the launched process group, not a foreign server, owns the endpoint."""

    process_group = os.getpgid(server.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = server.poll()
        if return_code is not None:
            raise RuntimeError(
                f"offline gate server exited before listening (exit {return_code})"
            )
        endpoint_inodes = _listener_inodes(host, port)
        if endpoint_inodes & _process_group_socket_inodes(process_group):
            return
        time.sleep(0.05)
    raise TimeoutError(
        f"listener at {host}:{port} was not owned by launched process group {process_group}"
    )


def _http(method: str, path: str, body: dict | None = None, timeout: float = 600) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method, headers={"content-type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            raw = response.read()
            content_type = response.headers.get("content-type", "")
            return response.status, (json.loads(raw) if "json" in content_type else raw)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _wait(path: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _http("GET", path, timeout=5)
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    raise TimeoutError(path)


def _poll(path: str, done: callable, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = _http("GET", path)
        if status == 200 and done(body):
            return body
        time.sleep(1)
    raise TimeoutError(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--console-index", type=Path)
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve(strict=True)
    repository_root = args.repository_root.resolve(strict=True)
    console_index = (
        args.console_index or repository_root / "frontend" / "dist" / "index.html"
    ).resolve(strict=True)
    expected_console = console_index.read_bytes()
    expected_console_sha256 = hashlib.sha256(expected_console).hexdigest()
    env = dict(os.environ)
    env.update({"QUALSIGHT_REPOSITORY_ROOT": str(repository_root), "QUALSIGHT_DATA_DIR": str(args.data_dir), "QUALSIGHT_NETWORK_GUARD": "1", "PYTHONPATH": f"{source_root / 'backend' / 'src'}:{source_root}",
                "QUALSIGHT_INPUT_RESOURCE_IDS": "ofiq-r-01-conformance-input,lfw-funneled-local-candidate"})
    server_command = (
        [str(args.python.resolve(strict=True)), "-m", "qualsight.cli", "serve"]
        if args.python is not None
        else [UV, "run", "--frozen", "--offline", "--directory", "backend", "python", "-m", "qualsight.cli", "serve"]
    )
    observation: dict = {"kind": "QUALSIGHT_OFFLINE_GATE", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": [], "data_dir": str(args.data_dir)}
    ok = True
    server: subprocess.Popen[bytes] | None = None

    def step(name: str, condition: bool, **facts: object) -> None:
        nonlocal ok
        ok = ok and condition
        observation["steps"].append({"step": name, "ok": bool(condition), **facts})

    try:
        _assert_endpoint_available(API_HOST, API_PORT)
        server = subprocess.Popen(server_command, cwd=repository_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        _wait_for_owned_listener(server, API_HOST, API_PORT, timeout=180)
        _wait("/api/health", 180)
        status, health = _http("GET", "/api/health")
        step("health", status == 200 and health["network_guard"]["installed"] is True, network_guard=health["network_guard"], actor=health["actor_id"], application_version=health["application_version"])
        status, page = _http("GET", "/")
        observed_console_sha256 = (
            hashlib.sha256(page).hexdigest() if isinstance(page, bytes) else None
        )
        step(
            "console-served-from-committed-stage",
            status == 200 and observed_console_sha256 == expected_console_sha256,
            bytes=len(page),
            expected_path=str(console_index),
            expected_sha256=expected_console_sha256,
            observed_sha256=observed_console_sha256,
        )
        status, project = _http("POST", "/api/projects", {"name": "Offline gate", "description": "QS-H72 offline operation gate", "purpose_scope": "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", "tags": [], "notes": []})
        step("project-created", status in (200, 201), project_id=project.get("project_id"))
        project_id = project["project_id"]
        _http("POST", f"/api/projects/{project_id}/open", {})
        status, dataset = _http("POST", f"/api/projects/{project_id}/datasets/import-registered", {"resource_id": "lfw-funneled-local-candidate", "verify_hashes": False})
        step("lfw-imported", status in (200, 201), dataset_id=dataset.get("dataset_id"), samples=dataset.get("sample_count"))
        dataset_id = dataset["dataset_id"]
        status, samples = _http("GET", "/api/samples/registered")
        r01 = next((s for s in samples if s["resource_id"] == "ofiq-r-01-conformance-input"), None)
        status, assessed = _http("POST", "/api/quality/assess", {"image_path": r01["path"], "purpose_scope": "REPRODUCE_OFIQ_CONFORMANCE_TESTS", "project_id": None})
        step("single-image-assessment", status == 200 and assessed.get("kind") == "ENGINE_MEASUREMENT", run_id=assessed.get("run_id"), unified_quality_score=next((m["scalar"] for m in assessed.get("measures", []) if m["name"] == "UnifiedQualityScore"), None))
        status, job = _http("POST", f"/api/projects/{project_id}/datasets/{dataset_id}/quality-batch", {"selection": {"kind": "subset_fixture"}, "chunks": 1})
        step("batch-started", status == 202, job_id=job.get("job_id"))
        job = _poll(f"/api/jobs/{job['job_id']}", lambda body: bool(body.get("ended_at")), 900)
        step("batch-completed", job["state"] == "COMPLETED", measured=job["assessed"], failed=job["failed"])
        status, protocol = _http("POST", f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2", {})
        step("protocol-frozen", status in (200, 201), protocol_id=protocol.get("protocol_id"), pairs=protocol.get("pair_count"))
        status, external = _http("POST", f"/api/projects/{project_id}/protocols/{protocol['protocol_id']}/import-external-scores", {"resource_id": "external-scores-lfw-view2-insightface-buffalo-l"})
        step("external-scores-imported", status == 201 and external.get("provenance_marker") == "EXTERNALLY_COMPUTED", leaf_id=external.get("leaf_id"), run_id=external.get("run_id"), scored=external.get("scored_count"), failed=external.get("failed_count"), eer=(external.get("eer") or {}).get("EXCLUDE_FAILED_TRIALS", {}).get("eer"), mapping=external.get("mapping"))
        beta_leaves = ",".join(BETA_REQUIRED_LEAVES)
        status, matrix = _http("POST", f"/api/projects/{project_id}/protocols/{protocol['protocol_id']}/matrix?fixture_pairs_only=true&leaves={beta_leaves}", {})
        step("matrix-started", status == 202, matrix_id=matrix.get("matrix_id"))
        matrix = _poll(f"/api/matrices/{matrix['matrix_id']}", lambda body: body["progress"]["phase"] in {"completed", "failed"}, 1800)
        step("matrix-completed", matrix["progress"]["phase"] == "completed" and len(matrix["leaves"]) == len(BETA_REQUIRED_LEAVES), leaves=len(matrix["leaves"]), required_leaves=list(BETA_REQUIRED_LEAVES))
        leaf = matrix["leaves"][0]
        status, utility = _http("POST", f"/api/projects/{project_id}/utility-analyses", {"matcher_run_id": leaf["run_id"], "quality_job_id": job["job_id"], "matcher_threshold": 0.8, "quality_threshold": 40.0, "bootstrap_replicates": 100, "seed": 5})
        step("utility-started", status in (200, 202), analysis_id=utility.get("analysis_id"))
        utility = _poll(f"/api/utility-analyses/{utility['analysis_id']}", lambda body: body["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}, 600)
        step("utility-completed", utility["state"] == "COMPLETED", run_id=utility.get("summary", {}).get("run_id"))
        status, cross = _http("POST", f"/api/projects/{project_id}/cross-matcher", {"matrix_id": matrix["matrix_id"], "quality_job_id": job["job_id"], "bootstrap_replicates": 100, "seed": 5})
        step("cross-matcher-started", status in (200, 202), analysis_id=cross.get("analysis_id"))
        cross = _poll(f"/api/cross-matcher/{cross['analysis_id']}", lambda body: body["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}, 900)
        step("cross-matcher-completed", cross["state"] == "COMPLETED" and len(cross["document"]["contrasts"]) == 9, contrasts=len(cross.get("document", {}).get("contrasts", [])))
        first_contrast = cross["document"]["contrasts"][0]["contrast_id"]
        status, disagreements = _http("GET", f"/api/cross-matcher/{cross['analysis_id']}/pairs?contrast_id={first_contrast}&limit=5")
        step("disagreements-resolve-to-pairs", status == 200 and disagreements["total"] > 0 and all(row["links"]["left_sample"] and row["links"]["run_a"] for row in disagreements["rows"]), contrast=first_contrast, pairs=disagreements.get("total"))
        status, figure = _http("POST", f"/api/projects/{project_id}/figures", {"kind": "det_overlay", "parameters": {"matrix_id": matrix["matrix_id"]}})
        step("figure-rendered", status == 201, figure_id=figure.get("figure_id"), sha256=figure.get("sha256"))
        status, dashboard = _http("POST", f"/api/projects/{project_id}/dashboards", {"name": "Offline gate dashboard", "template": "Cross-Matcher Evaluation", "columns": 2, "panels": [{"kind": "figure", "ref": figure["figure_id"]}, {"kind": "operating_points", "ref": leaf["run_id"]}, {"kind": "operating_points", "ref": external["run_id"]}, {"kind": "cross_contrasts", "ref": cross["analysis_id"]}, {"kind": "policy_table", "ref": utility["analysis_id"]}]})
        status2, reloaded = _http("GET", f"/api/dashboards/{dashboard.get('dashboard_id', '')}") if status == 201 else (0, {})
        step("dashboard-saved-and-reloaded", status == 201 and status2 == 200 and reloaded.get("layout_sha256") == dashboard.get("layout_sha256"), dashboard_id=dashboard.get("dashboard_id"), panels=len(reloaded.get("panels", [])))
        status, report = _http("POST", f"/api/projects/{project_id}/reports", {"spec": {"title": "Offline gate report", "dataset_id": dataset_id, "protocol_id": protocol["protocol_id"], "quality_job_id": job["job_id"], "matrix_id": matrix["matrix_id"], "utility_analysis_ids": [utility["analysis_id"]], "cross_matcher_analysis_id": cross["analysis_id"], "figure_ids": [figure["figure_id"]], "failure_examples": {"analysis_id": cross["analysis_id"], "contrast_id": first_contrast, "category": "NEITHER", "limit": 2}, "image_policy": "INCLUDE_PERMITTED"}})
        step("report-built", status == 201, report_id=report.get("report_id"), pdf_sha256=report.get("files", {}).get("pdf", {}).get("sha256"))
        status, bundle = _http("POST", f"/api/projects/{project_id}/bundles", {"run_id": utility["summary"]["run_id"]})
        step("bundle-exported", status == 201, bundle_id=bundle.get("bundle_id"), sha256=bundle.get("sha256"))
        status, reproduced = _http("POST", "/api/reproductions", {"bundle_path": bundle["path"]})
        step("reproduced", status == 201 and reproduced.get("state") in {"EXACT", "WITHIN_TOLERANCE"}, state=reproduced.get("state"), compared_values=reproduced.get("compared_values"))
        status, scope = _http("GET", f"/api/projects/{project_id}/purge-scope")
        step("purge-scope-listed", status == 200, files=len(scope["targets"]["files"]), directories=len(scope["targets"]["directories"]))
        status, audit = _http("GET", f"/api/projects/{project_id}/audit")
        step("audit-attributed", status == 200 and all(event["actor_id"] == health["actor_id"] and event["occurred_at"] for event in audit), events=len(audit), event_types=sorted({event["event_type"] for event in audit}))
        status, health_after = _http("GET", "/api/health")
        step("network-guard-refused-nothing", health_after["network_guard"]["installed"] and health_after["network_guard"]["refused_attempts"] == [], refused=health_after["network_guard"]["refused_attempts"])
        observation["project_id"] = project_id
    except Exception as error:  # noqa: BLE001 - recorded as evidence
        ok = False
        observation["error"] = f"{type(error).__name__}: {error}"
    finally:
        if server is not None:
            try:
                os.killpg(os.getpgid(server.pid), signal.SIGTERM)
                server.wait(timeout=30)
            except Exception:  # noqa: BLE001
                server.kill()
            observation["server_log_tail"] = (server.stdout.read().decode("utf-8", "replace")[-3000:] if server.stdout else "")
        else:
            observation["server_log_tail"] = ""
        observation["exit_code"] = 0 if ok else 1
        observation["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        args.observation.parent.mkdir(parents=True, exist_ok=True)
        args.observation.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"offline gate written to {args.observation} exit={observation['exit_code']}")
    return observation["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
