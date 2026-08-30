# QualSight hardening and optimization implementation plan

1. **COMPLETED — Cross-process data-directory ownership**
   - Add a real OS-level exclusive lease held by API lifetime and every non-server CLI invocation.
   - Prove contention and release with real subprocesses, then prove server-versus-CLI refusal.
2. **COMPLETED — Durable operation admission and restart reconciliation**
   - Persist operation intent before output creation/enqueue.
   - Reconcile interrupted quality, matrix, utility, cross-matcher, degradation, and identification work.
3. **COMPLETED — Crash-recoverable purge**
   - Journal intent and per-phase outcome; keep project blocked until one truthful terminal attestation exists.
4. **COMPLETED — Request and work backpressure**
   - Stream-enforce upload bytes and bound pending scientific jobs with deterministic overload responses.
5. **COMPLETED — Runtime operations hardening**
   - Separate liveness/readiness, bound drain, add native deadlines/output caps and container resource limits.
6. **COMPLETED — Real-data performance closure**
   - Establish current twelve-leaf baseline, then test condition-at-a-time preprocessing, exact blockwise top-k, cumulative metric sweeps, bounded detector parallelism, and streaming exports one axis at a time.
7. **COMPLETED — Assurance and release boundary**
   - Refresh dependency/security documentation, append audit evidence, rebuild Graft, and complete all final gates plus hostile diff review.
