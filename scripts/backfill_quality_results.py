"""Backfill the quality_results cache from a completed batch job's RECORDED parquet.

The quality_results table is a derived cache over each batch's immutable
``quality_measurements.parquet`` (a hashed Run artifact). Batches recorded
before the table existed have no cache rows, so per-sample quality lookups
(sample inspector, match-evidence panel) come up empty for them. This script
re-derives the rows from the recorded parquet bytes — after re-verifying the
parquet against the job's recorded SHA-256. Nothing is computed or invented;
INSERT OR IGNORE keeps the operation idempotent.

Usage: env PYTHONPATH=src:.. uv run --directory backend --frozen --offline \
           python ../scripts/backfill_quality_results.py [--data-dir var]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var")
    args = parser.parse_args()
    database = args.data_dir / "qualsight.sqlite3"
    connection = sqlite3.connect(str(database))
    jobs = connection.execute(
        "SELECT job_id, dataset_id, run_id, parquet_path, parquet_sha256 FROM jobs WHERE state = 'COMPLETED' AND parquet_path IS NOT NULL"
    ).fetchall()
    from qualsight.engines.ofiq.batch import MEASURE_NAMES

    total_inserted = 0
    for job_id, dataset_id, run_id, parquet_path, parquet_sha in jobs:
        cached = connection.execute("SELECT COUNT(*) FROM quality_results WHERE job_id = ?", (job_id,)).fetchone()[0]
        if cached:
            continue
        path = Path(parquet_path)
        if not path.is_file():
            print(f"skip {job_id[:28]}…: recorded parquet missing on disk")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if parquet_sha and digest != parquet_sha:
            print(f"REFUSED {job_id[:28]}…: parquet drifted from its recorded hash")
            continue
        rows = pq.read_table(parquet_path).to_pylist()
        payload = []
        for row in rows:
            measures = {
                name: {"native": row.get(f"{name}.native"), "scalar": row.get(f"{name}.scalar"), "failed": bool(row.get(f"{name}.failed"))}
                for name in MEASURE_NAMES
            }
            uqs = measures.get("UnifiedQualityScore", {})
            payload.append((
                job_id, row["sample_id"], dataset_id, run_id, row["outcome"], row.get("failure_reason"),
                uqs.get("scalar"), uqs.get("native"),
                json.dumps(measures, sort_keys=True),
            ))
        connection.executemany(
            "INSERT OR IGNORE INTO quality_results (job_id, sample_id, dataset_id, run_id, outcome, failure_reason, uqs_scalar, uqs_native, measures_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        connection.commit()
        total_inserted += len(payload)
        print(f"backfilled {len(payload)} rows for {job_id[:28]}… (parquet sha verified)")
    print(f"done: {total_inserted} rows inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
