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
| Matcher matrix (12 leaves × 6,000 pairs, 7,701 images, GPU) | 448.115 s mean before completed-job release; 449.741 s after | real LFW View 2, six exact matchers × two SCRFD conditions, two detector workers; evidence below | throughput unchanged (+0.36%); release completed job controllers to stop retained matrix memory |
| Console bundle | main JS 352.84 KB, 95.05 KB gzip | Vite 7.3.6 production build after the vulnerability closure | no code-splitting justified for a local single-user console |

Known larger cost, deliberately unoptimized: bundle **reproduction** of a matcher Run re-embeds
every image (7,701 embeds ≈ minutes on GPU). Reproduction is a verification act — recomputing from
the original bytes is the point; caching embeddings would verify the cache, not the pipeline.

## 2026-08-28 full-matrix optimization closure

Both records execute the same registered 7,701-image LFW input, 6,000 View 2 pairs, six exact
checkpoint identities, and twelve matcher/preprocessing leaves in one API process. They are dirty-tree
engineering measurements bound to commit `8abdf3eac6bdf91e5a347fe3e50bab172c83f052`; they are not release
benchmarks. Each comparison table and embedding artifact is produced by the real pipeline.

| Evidence | Repetitions | Wall seconds | Process peak RSS | Resident-memory progression |
|---|---:|---:|---:|---|
| `docs/performance/2026-08-28_full-lfw-twelve-leaf.json` (SHA-256 `1d6294db…`) | 3 | 447.470, 447.388, 449.486; mean 448.115, sample SD 1.188 | max 4,210,278,400 bytes | before 774,692,864 → after 2,952,654,848; then +259,911,680; then +347,471,872 bytes |
| `docs/performance/2026-08-28_full-lfw-twelve-leaf-post-retention.json` (SHA-256 `2f3f5603…`) | 2 | 449.636, 449.846; mean 449.741, sample SD 0.148 | max 3,918,430,208 bytes | before 775,860,224 → after 2,822,144,000; second repetition +30,216,192 bytes |

The first repetition necessarily establishes model/runtime caches. The diagnostic signal is the
subsequent repetition: retained RSS growth fell from 259–347 MB per completed matrix to 30 MB after
the API removed terminal `MatcherMatrixJob` controllers from its in-memory map. Peak RSS fell by
291,848,192 bytes while mean wall time changed by 1.626 seconds. Matrix files, Runs, audit events, and
operation records remain durable and queryable; only the completed Python controller object is released.

The runtime also uses exact blockwise stable 1:N ranking, cumulative verification sweeps, streaming
exports, condition-at-a-time memory-mapped embeddings, and a bounded two-worker detector stage. Their
real equivalence gates are part of backend discovery. The separate instrumented record
`docs/performance/2026-08-28_full-lfw-twelve-leaf-vram.json` (SHA-256 `a76b2069…`) completed the same
12 leaves in 451.564 seconds with peak process RSS 3,705,274,368 bytes and peak process GPU VRAM
2,571,108,352 bytes. It is reported separately so adding repeated `nvidia-smi` sampling is not merged
into the before/after RSS comparison.
