#!/usr/bin/env python3
"""Run and record repeated full LFW twelve-leaf matcher benchmarks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.matchers.runtime import MatcherId


ROOT = Path(__file__).resolve().parents[1]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


def _rss_bytes() -> int:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("/proc/self/status does not report VmRSS")


def _command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


def _gpu_vram_bytes() -> int:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    process_id = os.getpid()
    for line in result.stdout.splitlines():
        pid_text, separator, memory_text = line.partition(",")
        if separator and int(pid_text.strip()) == process_id:
            return int(memory_text.strip()) * 1024 * 1024
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        raise ValueError("repetitions must be positive")
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite benchmark record: {args.output}")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if any(args.data_dir.iterdir()):
        raise ValueError(f"benchmark data directory must be empty: {args.data_dir}")

    settings = Settings(
        repository_root=ROOT,
        registry_path=ROOT / "resources" / "resource-registry.json",
        data_dir=args.data_dir,
        runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
        input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        scientific_workers=1,
        scientific_pending=0,
        detector_workers=2,
    )
    stop_sampling = threading.Event()
    peak_rss = {"bytes": _rss_bytes()}
    peak_gpu_vram = {"bytes": _gpu_vram_bytes()}

    def sample_memory() -> None:
        while not stop_sampling.wait(0.5):
            peak_rss["bytes"] = max(peak_rss["bytes"], _rss_bytes())
            peak_gpu_vram["bytes"] = max(peak_gpu_vram["bytes"], _gpu_vram_bytes())

    sampler = threading.Thread(target=sample_memory, name="qualsight-benchmark-rss", daemon=True)
    sampler.start()
    started_at = datetime.now(timezone.utc).astimezone().isoformat()
    records: list[dict[str, object]] = []
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Full LFW twelve-leaf performance benchmark",
                    "description": "Repeated real-data benchmark of every admitted matcher and preprocessing leaf.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            project.raise_for_status()
            project_id = project.json()["project_id"]
            client.post(f"/api/projects/{project_id}/open").raise_for_status()
            imported = client.post(
                f"/api/projects/{project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            imported.raise_for_status()
            dataset_id = imported.json()["dataset_id"]
            frozen = client.post(
                f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
            )
            frozen.raise_for_status()
            protocol_id = frozen.json()["protocol_id"]
            acquisition: list[dict[str, object]] = []
            for matcher_id in MatcherId:
                source_pull = client.post(
                    f"/api/matcher-assets/{matcher_id.value}/pull-sources"
                )
                if source_pull.status_code != 200:
                    raise RuntimeError(
                        f"source pull failed for {matcher_id.value}: {source_pull.text}"
                    )
                checkpoint_pull = client.post(
                    f"/api/matcher-assets/{matcher_id.value}/pull-checkpoint"
                )
                if checkpoint_pull.status_code != 200:
                    raise RuntimeError(
                        f"checkpoint pull failed for {matcher_id.value}: {checkpoint_pull.text}"
                    )
                acquisition.append(
                    {
                        "matcher_id": matcher_id.value,
                        "source_event": source_pull.json()["event"]["system_event_id"],
                        "checkpoint_receipt": checkpoint_pull.json()["receipt"]["receipt_id"],
                        "checkpoint_sha256": checkpoint_pull.json()["receipt"]["artifact"]["sha256"],
                    }
                )
            for repetition in range(1, args.repetitions + 1):
                rss_before = _rss_bytes()
                started = time.perf_counter()
                launched = client.post(
                    f"/api/projects/{project_id}/protocols/{protocol_id}/matrix"
                )
                launched.raise_for_status()
                matrix_id = launched.json()["matrix_id"]
                deadline = time.monotonic() + 7200
                while time.monotonic() < deadline:
                    status = client.get(f"/api/matrices/{matrix_id}")
                    status.raise_for_status()
                    body = status.json()
                    if body["progress"]["phase"] in {"completed", "failed"}:
                        break
                    time.sleep(1.0)
                else:
                    raise TimeoutError(f"matrix {matrix_id} exceeded the 7200-second benchmark deadline")
                if body["progress"]["phase"] != "completed":
                    raise RuntimeError(f"matrix {matrix_id} failed: {body['progress']}")
                leaves = body["leaves"]
                records.append(
                    {
                        "repetition": repetition,
                        "matrix_id": matrix_id,
                        "wall_seconds": round(time.perf_counter() - started, 3),
                        "rss_before_bytes": rss_before,
                        "rss_after_bytes": _rss_bytes(),
                        "process_peak_rss_bytes": peak_rss["bytes"],
                        "process_peak_gpu_vram_bytes": peak_gpu_vram["bytes"],
                        "pairs": launched.json()["pairs"],
                        "samples": launched.json()["samples"],
                        "leaves": len(leaves),
                        "run_ids": [leaf["run_id"] for leaf in leaves],
                        "leaf_timings": {
                            leaf["leaf_id"]: {
                                "created_at": leaf["created_at"],
                                "comparison_count": leaf["comparison_count"],
                                "scored_count": leaf["scored_count"],
                                "failed_count": leaf["failed_count"],
                            }
                            for leaf in leaves
                        },
                    }
                )
    finally:
        stop_sampling.set()
        sampler.join(timeout=5)

    record = {
        "kind": "QUALSIGHT_REAL_MATRIX_BENCHMARK_V1",
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "repository_commit": _command(["git", "rev-parse", "HEAD"]),
        "repository_dirty": bool(_command(["git", "status", "--porcelain"])),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
                if line.startswith("model name")
            ),
            "unreported",
        ),
        "gpu": _command(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]),
        "matcher_device": os.environ.get("QUALSIGHT_MATCHER_DEVICE", "auto"),
        "detector_workers": settings.detector_workers,
        "scientific_workers": settings.scientific_workers,
        "data_dir": str(args.data_dir.resolve()),
        "registry_mtime_ns": settings.registry_path.stat().st_mtime_ns,
        "lfw_fixture_mtime_ns": (ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").stat().st_mtime_ns,
        "repetitions": records,
        "acquisition": acquisition,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
