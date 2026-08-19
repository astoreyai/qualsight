# QualSight performance baselines (QS-S98)

Measured on the reference workstation (32-core, RTX GPU shared with other users), 2026-08-19.
Rule: no optimization ships without a recorded baseline that justifies it and a re-measurement
that proves it. Numbers below are the recorded evidence for the current decision to **not**
optimize further.

| Path | Measured | Basis | Decision |
|---|---|---|---|
| OFIQ batch throughput | ≈ 2.3 images/s, one engine process | decision 22 (chunk benchmark: 1 proc 2.3 > 2 procs 2.0 > 6 procs 1.8 aggregate — engine saturates the cores) | engine-bound; `chunks=1` stays the default |
| Single upload assessment (`assess-upload`) | ≈ 3–5 s end to end | engine init ≈ 2.9 s + assess ≈ 0.6 s (observation 12, real Run) | acceptable for the live demo moment; init dominates and is the engine's own cost |
| OFIQ internal landmarks (`/api/ofiq-landmarks`) | ≈ 3.3 s | full engine assessment inside the registered auxiliary binary (Z90 smoke run, wall clock) | inherent — the public API runs the whole assessment before exposing preprocessing results |
| Pair evidence (`/api/.../evidence`) | ≈ 0.2 s | 6,000-row comparisons parquet `to_pylist` 0.054 s + per-job measure medians 0.10 s / 1,937 rows (measured directly on the persistent store) | no cache justified at current sizes |
| SCRFD detection endpoint | < 0.5 s | CPU ONNX detector, single image (C28 test wall clock) | fine |
| Matcher matrix (6 leaves × 6,000 pairs, GPU) | ≈ 25–40 min recorded | beta matrix Runs (GPU contention dependent) | recorded per Run; GPU headroom fallback (V13) governs device choice |
| Console bundle | 1.2 MB dist; main JS 266 KB (pre-gzip) | `du`/`ls` on `frontend/dist` after the routed-navigation build | no code-splitting justified for a local single-user console |

Known larger cost, deliberately unoptimized: bundle **reproduction** of a matcher Run re-embeds
every image (7,701 embeds ≈ minutes on GPU). Reproduction is a verification act — recomputing from
the original bytes is the point; caching embeddings would verify the cache, not the pipeline.
