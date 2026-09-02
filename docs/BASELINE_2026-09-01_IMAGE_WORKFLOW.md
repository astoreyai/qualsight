# Image-workflow repair baseline — 2026-09-01

This is the frozen pre-change baseline for the recorded image-workflow repair. Commands were run from
`/mnt/projects/ofiqgui` at Git commit `fb159edc0a069151871b8bcba2fb460352a50e32` on branch `main`.
The tracked tree was clean. `.playwright-mcp/` was already untracked and was not read, changed, staged,
or treated as release input.

## Source and real-data identities

| Input | Pre-change evidence |
|---|---|
| OFIQ conformance image | `/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png`; 13,436,071 bytes; mtime `2023-04-18 07:31:34 -0500` |
| LFW pair manifest | `resources/manifests/lfw-local-pairs-v1.jsonl`; 2,800,299 bytes; mtime `2026-08-14 15:56:30.752553445 -0500` |
| Concept A | SHA-256 `0d7023fe53e5672c9841f8114825e9b72047008163dc3f403d3a5ee2b8860067` |
| Concept B | SHA-256 `982b40b763e7d45746545f88c594004f2c6b94cf9096ddaa82c752eebd155aaa` |
| Concept C | SHA-256 `77879cbae10f1c2890325f3354fd26ce34a2df5b70b4d66f271be0a4807608a1` |

## Test and build baseline

| Gate | Command | Result |
|---|---|---|
| Root governance and protocol tests | `.venv/bin/python -m unittest discover -s tests -q` | PASS, 60/60 in 69.260 seconds; no failures or errors |
| Backend real-engine suite | `env PYTHONPATH=src:.. ~/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -q` | PASS, 184 tests in 1,634.986 seconds; 2 skipped; no failures or errors |
| Frontend toolchain | pinned PATH with `/usr/bin/node`; `pnpm run verify:toolchain` | PASS, Node 20.20.2 and pnpm 10.28.0 |
| Frontend typecheck | pinned PATH; `pnpm run typecheck:frontend` | PASS |
| Frontend production build | pinned PATH; `pnpm run build:frontend` | PASS, 41 modules; 352.84 kB main JavaScript and 21.72 kB main CSS before gzip |

The two backend skips are the opt-in network gates
`OfficialVerificationDatasetNetworkGate.test_archive_downloads_validates_and_executes_every_matcher`
and
`OfficialElasticFaceCheckpointNetworkGate.test_official_endpoint_streams_the_registered_bytes_without_environment_proxy`.
Their 249,538,560-byte dataset archive and 261,216,371-byte checkpoint downloads are intentionally not
run by the offline baseline.

The first frontend invocation selected the user's Node 26.7.0 and was rejected by pnpm before running
the build. Re-running with the repository-authoritative `/usr/bin/node` 20.20.2 passed. The first backend
invocation supplied a redundant discovery top directory and was rejected by `unittest` before loading
tests. The documented command above passed. These were invocation failures, not code failures, and are
retained here so the release commands use the pinned runtime and correct discovery root.

## Route, API, and browser baseline

- The running pre-change application reported QualSight `0.1.0b2` and OFIQ `1.1.2` healthy at
  `127.0.0.1:8480` before the baseline suite acquired the data-directory lease.
- The pre-change navigation exposed nine top-strip destinations beginning with Projects. The failing
  navigation acceptance run on this frozen tree exited at `scripts/observe_console.py:155` because the
  accepted task shell requires Home followed by the full ten-destination task sequence and a left-rail
  geometry.
- A registered real sample displayed 28 OFIQ measures, five SCRFD points, and 98 OFIQ landmarks in the
  existing browser audit.
- A real owner upload produced a recorded Run, sample, canonical stored path, dimensions, hash,
  provenance, and 28 measures at the API. The pre-change `runOwnerUpload` state did not select that
  recorded sample, so the inspector preview and overlay controls remained bound to the prior registered
  selection. The dedicated test-first browser gate records this failure separately.
- Utility accepted a free-text measure identifier. The backend rejected an unrecorded scalar measure
  during analysis admission, but no endpoint or selector exposed the 28 scalar columns recorded by a
  selected completed quality job. The pre-change focused test received HTTP 404 from the new catalog
  route.

The detailed pre-change interaction audit is
`/home/aaron/Desktop/2026-09-01_qualsight_image_workflow_ui_audit.md`. It is local review evidence and is
not a release-archive input.

## Performance and release baseline

The latest recorded full-LFW post-retention benchmark is
`docs/performance/2026-08-28_full-lfw-twelve-leaf-post-retention.json`. It records 12
matcher/preprocessing leaves over 7,701 samples and 6,000 pairs for each of two repetitions. Wall times
were 449.636 and 449.846 seconds; process peak RSS was 3,918,430,208 bytes. The machine record names an
Intel Core i9-14900K and an NVIDIA GeForce RTX 3090 with 24,576 MiB. That benchmark was produced from a
dirty `8abdf3e…` tree and is a performance reference, not release proof.

The installed release baseline is `dist/qualsight-0.1.0b2`. Its fail-closed record reports 13/13 PASS at
commit `f27a62794048792c46f83923bb899c70ce52d4a3`, tree
`f0fd80f0b81905b71ad96e848ed420a7abf8c629`, under
`CONTROLLED_SINGLE_USER_WORKSTATION_BETA`. A repaired package must use a new explicit version and must not
overwrite this directory.
