# QualSight Development Graph Plan

**Graph version:** v1.6  
**Date:** August 17, 2026  
**State:** Phases 0, 1, 2, and 3 are complete (Phase 2 under owner decision 28): QS-A00 through QS-A10, QS-B10 through QS-B14, QS-C20, QS-C22, QS-C23, QS-C24, QS-C25, and QS-D30 through QS-D34 are accepted whole capabilities; current evidence children QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, and QS-D31.R0 are accepted; refinement parent QS-A02.LFW is accepted; QS-C21 remains blocked on a real external score artifact and is parked as a direct predecessor of QS-H74; QS-E40 is dependency-ready and Phases 4, 5, and 7 are the owner-selected next work  
**Loop controller:** [ATG_LOOP.md](./ATG_LOOP.md)  
**Canonical product requirements:** [PRD.md](./PRD.md)  
**Evidence-only companion:** [PLAN.md](./PLAN.md)

This is the live dependency graph for developing QualSight. Coarse capability nodes are not executable leaves. Before a capability runs, ATG_LOOP.md requires it to be refined into literal invocations with real inputs and measurable acceptance predicates.

## L-1 — Decision record

1. PRD.md v0.3 controls scope.
2. PLAN.md v0.2 contributes only validated research and integration evidence. Its live-capture, kiosk, mobile, SQLite-for-analytical-results, and synthetic-dataset content does not control implementation.
3. /mnt/projects/ofiqgui is the QualSight implementation root.
4. Python 3.11.2, uv 0.9.26, Node 20.20.2, and pnpm 10.28.0 are the accepted project toolchain. Exact project controls are checked in; dependency selection remains a per-slice engineering decision and must be justified before packages are added.
5. OFIQ 1.1.0 is historical evidence only. The forward engine is the official OFIQ v1.1.2 tag at commit `d589ca27896a472446abc9eca8b62947ee47db19`, built without sudo in the isolated sibling root `/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-build` by `scripts/ofiq_v112_build.sh` from the CMake-selected archives, installed under that root, and registered as `ofiq-v1-1-2-linux-runtime`; only that registered installed runtime executes for QualSight.
6. General scientific work uses approved real data only. The OFIQ conformance images are restricted to conformance reproduction.
7. The initial beta path is Phase 0, 1, 2, 3, 4, 5, then 7. Phase 6 advanced analysis is post-beta because its 1:N, demographic, PAD, and controlled-degradation scope is excluded from or deferred by the beta scope in PRD sections 5, 17, 25, 26, and 55.
8. Single-user mode is required. Local-account mode remains conditional and has no dependency edge into the beta gate.
9. Local beta matcher execution uses AdaFace IR-101, ElasticFace-Arc R100, and TransFace-S Glint360K. Each matcher must produce separate Runs for the two frozen preprocessing conditions defined below. Imported scores remain a separate required path and must remain permanently marked EXTERNALLY COMPUTED.
10. No commercial SDK is a beta dependency.
11. Project-owner authorization and third-party rights are independent. Owner approval cannot promote a resource whose rights, provenance, runtime closure, or executable validation remains incomplete.
12. Unpinned or unverified downloads, random initialization, unsafe checkpoint loading, and unregistered runtime bytes are prohibited. Acquisition happens only in an explicit setup step that pins the source and the SHA-256 identity and refuses on mismatch; scientific runtime makes no network call.
13. Matcher comparisons hold the preprocessing condition and protocol fixed; preprocessing comparisons hold the matcher, checkpoint, tensor transform, protocol, and score implementation fixed.
14. No source image, checkpoint, embedding, or restricted artifact is redistributed.
15. QS-C22.CROP.R0 may classify only the historical shared-crop producer behavior proven by preserved bytes. It cannot claim the full shared preprocessing condition until the ArcFace alignment template and authorized BGR decode plus matcher consumption are byte-bound.
16. QS-A02.LFW.P0 may bind only the exact official protocol documents, View 2 pair/fold definition, and conservative planned-study category proven by those bytes. It cannot claim image rights, historical dataset acquisition, matcher-training identity exclusion, complete official-protocol conformance, or execution authority.
17. QS-A02.SCRFD.P0 may bind only the official InsightFace v0.7 release metadata, current `buffalo_l` archive identity, exact ZIP membership, and byte identity of the registered `det_10g.onnx` member. It cannot reconstruct the original historical acquisition event, determine redistribution rights, validate a runtime, or by itself grant effective scope; effective scope came only from decision 18.
18. On 2026-08-17 the project owner granted local noncommercial research use of the registered LFW bytes, the OFIQ v1.1.2 source/model bundle/ONNX Runtime, the SCRFD-10G detector, and the three matcher checkpoints, with the instruction that QualSight never ships them and instead acquires them at setup time from official or mirror sources under pinned SHA-256 identities. The receipt `resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json` records the verbatim instruction. Owner authorization stays distinct from third-party rights: every resource keeps its factual `third_party_rights_status`, and the new registry state `APPROVED_LOCAL_RESEARCH_USE` requires an owner decision, non-empty effective scopes inside the owner grant, no redistribution, and a terminal (non-UNRESOLVED) rights status.
19. Matcher training-set exclusion of LFW test identities is recorded as each matcher's published-protocol claim, not as a QualSight-verified fact; every LFW result is reported under the official View 2 protocol as `UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA` with that caveat.
20. Timing values (initialization, assessment, wall time) are observations, never equality gates; a reproduction is EXACT when every non-timing metric of the same registered engine on the same registered bytes is equal.
21. Historical evidence whose mutable external source bytes change after acceptance is never re-pinned silently: the drift is recorded append-only with the historical and current identities, and the classification is re-derived from the current bytes only when the same predicates still hold (QS-C22.CROP.R0, generator source rewritten outside QualSight at 2026-08-17T07:23:09-05:00).
22. Batch quality execution runs one registered engine process per chunk through the same registry-bound CLI adapter, using OFIQSampleApp's list-file mode with a generated `.txt` list inside a job directory and an output CSV that must not pre-exist; on the 32-core workstation one process (≈2.3 images/s) is faster than two (≈2.0 images/s aggregate) or six (≈1.8) because the engine already saturates the cores, so `chunks=1` is the default and concurrency is an explicit choice, never an assumption.
23. Every batch row is one of `MEASURED`, `ENGINE_ROW_FAILURE`, `INTEGRITY_FAILURE`, or `NOT_ASSESSED`; a measure the engine reports as scalar `-1`/native `nan` stays an explicit measure failure inside a `MEASURED` row. Only a batch whose engine processes all exited 0 without cancellation is recorded as a `COMPLETED` Run; cancelled or failed batches keep their Parquet, list, and CSV evidence without a COMPLETED Run.
24. Import-time dataset statistics are immutable evidence (hash recorded); PRD §11 quality coverage is recomputed live from every recorded batch result and reported next to them, never merged into the import digest.
25. The frozen matcher runtime loads registered checkpoints only with `torch.load(weights_only=True)` and `load_state_dict(strict=True)` into architecture sources acquired at setup time from pinned upstream commits (verified by SHA-256, never bundled — TransFace's repository carries no license and ElasticFace's backbone is loaded from insightface's MIT `iresnet.py`); insightface's SCRFD decoding and five-point alignment are ported (MIT). Any key mapping, non-strict load, or random fallback is a defect, not a fallback.
26. Verification metrics are computed with the registry definitions and every required input made explicit — score direction, `ACCEPT_WHEN_SCORE_GE_THRESHOLD`, and two named failure policies (`EXCLUDE_FAILED_TRIALS` with FTA reported; `FAILURES_AS_REJECT`) reported side by side; EER is located by linear interpolation between adjacent recorded thresholds; uncertainty is Clopper–Pearson exact binomial with the stated independence caveat plus the View 2 ten-fold accuracy.
27. LFW results are reported as QualSight-executed on the funneled release with SCRFD alignment; they are never presented as reproductions of the matchers' published LFW figures.
28. Owner amendment 2026-08-17 (`resources/authorizations/qualsight-owner-phase2-amendment-2026-08-17.json`): Phase 2 closes on QualSight-executed Runs; QS-C21 (real external score importer) is parked as a required beta node feeding QS-H74 directly and accepts only when a real externally computed score artifact is registered — never a self-generated one. TransFace-S stays in the matrix under local noncommercial research use with setup-time acquisition. Commits to `main` are authorized; pushes are not until a remote is named.

## Confirmed baseline

### Planning sources

| Artifact | SHA-256 | Mtime |
|---|---|---|
| PRD.md | cbdd00e9a43c67ae1e803b08cb1729fae23f842a79e3be5e7f18246c3bbf3025 | 2026-08-13 17:06:54.266266242 -0500 |
| PLAN.md | dc6afaf12fc824709569500717170c2cc40cf32e97a49f7ae238d16f66bbe699 | 2026-08-13 17:06:54.918284036 -0500 |
| REQUIREMENTS.md | dc728cab65fed1786996c1fea9f6c36779bc89b75544f47c09dfa55756373beb | 2026-08-14 10:44:15.153204121 -0500 |

The root was confirmed as NOT_A_GIT_WORKTREE before these graph documents were created. An authorized initialization command created an empty Git repository on branch main at 2026-08-13 18:09:11 -0500. HEAD remains unborn and no files are staged or committed.

### Installed toolchain

| Tool | Confirmed version |
|---|---|
| Python | 3.11.2 |
| uv | 0.9.26 |
| Node | 20.20.2 |
| npm | 11.16.0 |
| pnpm | 10.28.0 |
| Docker | 29.7.2, build a7dcaa6 |
| Git | 2.39.5 |
| CMake | 3.26.6 |
| Conan | 2.18.1 (user-local `uv tool install`, installed 2026-08-17 by the OFIQ setup script) |
| Ninja | 1.13.0.git.kitware.jobserver-pipe-1 |
| GPU | NVIDIA GeForce RTX 3090, driver 535.261.03, 24576 MiB |

These versions are observed environment facts, not permanent minimum-version requirements.

### Accepted project toolchain controls

| Artifact | SHA-256 |
|---|---|
| .gitignore | 6d3cf3480e7795870dbfcb1bff6144241382a6e323c281cfd1966806b97b03d7 |
| .npmrc | 742d52454ef0f2d4672899c74bbf3d8396ca18eb07d7576ad9b0d76960da9097 |
| .python-version | c34502f7065215daf6747eb2c8697cec24c54e12442353802a8b99a3611d7941 |
| .node-version | 53e75c7a2135680425af70a1c40678498323acf8f30ec7b95c99d92e26713abb |
| pyproject.toml | b466f3316aaebb68d1240a97e5d410fcb08686af6be578b11520760a8e66c614 |
| backend/pyproject.toml | 7780b4260b3d211874f1d458c126e68f8e8e2e63be07f2db88bb19f3c6e0cb80 |
| uv.lock | 7c2cc110efc93a0147bcd1acdec24ec158f60c0368c6ebe98c95fd6eea01fdaa |
| package.json | 07b008098adcd1af3a6ad9e451af84f9f2c5ee01b2a112c5bfe77166c52916c9 |
| pnpm-workspace.yaml | ba941fbbc530ddb8e2f9a7a1c7ec04e2f4d2cb6fd6d068069d82e0a970043995 |
| frontend/package.json | 0935645a8e0524225309f0381680e90ac808823ef6c6f5f94b683b598c7c79cc |
| pnpm-lock.yaml | 07553cc9f135c78d1f5679ca0365f61a8a788dcd1a8b1aed23c1e91cade121f8 |

The Python workspace uses one uv lock, requires uv 0.9.26, disables Python downloads, and keeps the uv cache at `.cache/uv`. QS-A03 introduced `pydantic==2.13.4` for strict immutable domain validation; QS-A08 added the PRD §45 control plane `fastapi==0.141.1` and `uvicorn==0.52.3` (transitively `starlette==1.6.0`, `h11==0.16.0`, `idna==3.18`) plus the dev-only `httpx==0.28.1` used by the in-process test client; QS-B12/QS-B13 added the PRD §47 Parquet/analytics dependencies `pyarrow==25.0.1` and `duckdb==1.5.5`; QS-C22 added the frozen matcher runtime closure `torch==2.10.0` (cu128 wheel with its NVIDIA runtime libraries), `timm==1.0.28` (with `torchvision==0.25.0`), `onnxruntime==1.28.0`, `opencv-python-headless==5.0.0.93`, and `numpy==2.4.6`. The pnpm workspace uses one lock, requires pnpm 10.28.0, enforces Node 20.20.x and strict engines, keeps its store at `.cache/pnpm-store`, and approves only the `esbuild` build script; QS-A09 added the console dependencies `react==18.3.1`, `react-dom==18.3.1`, `vite==5.4.11`, `@vitejs/plugin-react==4.3.4`, `typescript==5.6.3`, `vite-plugin-pwa==0.21.1`, `workbox-window==7.3.0`, `@types/react==18.3.12`, and `@types/react-dom==18.3.1`. The accepted Node 20.20.2 is `/usr/bin/node`; since 2026-08-17 10:45 a `~/.local/bin/node` symlink to a v26.7.0 install created by an unrelated workflow shadows it, so QualSight Node commands run with `/usr/bin` first on `PATH` (the toolchain gate `pnpm run verify:toolchain` proves 20.20.2/10.28.0). The direct pnpm 10.28.0 executable is the accepted invocation.

### Initial real conformance resource

| Property | Confirmed value |
|---|---|
| Source directory | /mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images |
| Allowed use | Reproducing OFIQ conformance tests only |
| Image-directory inventory | 28 PNG files |
| Historical absolute-path image manifest SHA-256 | bb60cfa4c9cb8b788bedadb0535ca70f30f5f78b2979dc591a090d6b64c36485 |
| Portable C-locale, repository-relative image manifest SHA-256 | c6c575ecf37a4aff87c75394b6cab1b10f8caf56edda2c54bc2ba8a4bc341917 |
| License file SHA-256 | f2457086d49ff0dc0faab72cddc214a5119d321bb46e81e6ad19319063b635bc |
| Initial image | /mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png |
| Initial image SHA-256 | 3c3655e6ac9f16bc6909b6f71d7c5ec385008aa6d2352167bdb77acd61d8c947 |
| Registered initial-image manifest SHA-256 | 8d7a99ebf31cd0895b5a21d939e117d630a3045444456bee558457b5cb0c3663 |
| Initial image mtime | 2023-04-18 07:31:34.000000000 -0500 |

The historical `bb60c...` digest includes absolute `/mnt` paths and is location-dependent. The portable `c6c575...` inventory fixes `LC_ALL=C` and paths relative to the OFIQ repository root. The resource registry consumes and licenses only `r-01-frontal.png`; the other 27 files are inventory evidence, not authorized QualSight inputs. All images must remain in place, and the graph does not authorize copying or republishing them.

### Historical confirmed OFIQ 1.1.0 runtime

| Resource | Confirmed value |
|---|---|
| Source worktree commit | bb5dc91d00477e02ce53d2530d28e35021484393 |
| Source tag and remote | v1.1.0; https://github.com/BSI-OFIQ/OFIQ-Project.git |
| Worktree state | Dirty; build, models, external libraries, and conformance images are untracked |
| Engine-reported version | 1.1.0 |
| OFIQSampleApp SHA-256 | fb41aa5c819d5432aa94e29415a2e0e702a124cc2c3b4c6aca3cfb6cd0af20b9 |
| libofiq_lib.so SHA-256 | 5d90a2238fb8879f1eb6817ff1df705c16099f7452878189398fb091c0188664 |
| ofiq_config.jaxn SHA-256 | e117286706d799a1e23130db01cfdff7ef36d157b22ecf68dd196052a8a8d0b3 |
| ONNX Runtime library SHA-256 | 1147caa734d19f7549f90a659aa9ca444366eeb80a596900d19d242145a9f4c9 |
| Model-tree inventory | 24 files: 12 configured payloads, 10 license files, and 2 README files |
| Historical locale-bound model-tree manifest SHA-256 | 7cb65f7483cd598357eb8c5e0a43afcbb46e41436fb8332da1be8e47955a57ff |
| Portable C-locale, repository-relative model-tree manifest SHA-256 | 9d2ce53258af1e555724719255b30081c03e3995005e1686f6e58eb707fd3503 |
| Configured model-payload count | 12 |
| Configured model-payload manifest SHA-256 | fa908247460807c7deea039c8c8bf76f0964d7bc863cd9f510766c559a80b7e7 |

The binary fails to load without an explicit ONNX Runtime library path. With the path supplied, the real r-01-frontal.png run exited 0. Observed output included:

- initialization: 2486 ms;
- assessment: 325 ms;
- UnifiedQualityScore raw value: 26.390884;
- UnifiedQualityScore scalar: 79;
- wall time observed by the command runner: 3.0 s.

An exact-command verification rerun also exited 0 with 2436 ms initialization, 412 ms assessment, and 2.873 s runner wall time. The unchanged quality values and variable timing confirm why timing fields are observations rather than equality gates.

The QS-A02.R0 registry-cycle rerun exited 0 with engine version 1.1.0, 2182 ms initialization, 273 ms assessment, UnifiedQualityScore raw value 26.390884, and scalar 79. The build-tree executable still needs the explicit ONNX Runtime path because its RUNPATH contains obsolete build-host paths. A separate installed-tree executable has different hashes and has not been execution-validated for QualSight.

The portable manifests use C-locale byte ordering and lines of `SHA-256`, two spaces, and OFIQ-repository-relative path. The tagged v1.1.0 CMake configuration names the ISO edition-1 model bundle, while the tracked model README names edition 2. The historical local bytes did not preserve their acquisition archive or retrieval event. The v1.1.0 runtime is BLOCKED with owner decision `NOT_APPROVED`; current gates invoke it only to prove pre-launch refusal, never to execute it. QualSight execution uses the registered v1.1.2 installed runtime described below.

### Confirmed OFIQ v1.1.2 acquisition evidence

| Property | Confirmed value |
|---|---|
| Source clone | Clean official tag `v1.1.2`, commit `d589ca27896a472446abc9eca8b62947ee47db19` |
| Acquisition receipt SHA-256 | `c0c34b79c37bb4b27e1f005cab96970abd1ad99e78afcbc56697bd369272cac8` |
| Receipt evidence boundary | `PROJECT_RECORDED_WITHOUT_RAW_TRANSFER_LOG` |
| CMake-selected model archive SHA-256 | `8ceb6cec30c95056044e9191bbf8aa26e3e29a639b40a7636a60e13063987f36` |
| CMake-selected ONNX Runtime archive SHA-256 | `a0994512ec1e1debc00c18bfc7a5f16249f6ebd6a6128ff2034464cc380ea211` |
| Model quarantine tree SHA-256 | `651198b9d2dbc4ca3d9f6d03dbe94b054ae5f2018927613ba635ca76ea9c2c96` |
| Root-normalized 24-file model manifest SHA-256 | `f25e7823837a5aefabf5e75eee6a6a237c91ce4a7cb725b9d1531bc6a63093a7` |
| Tagged-config 12-payload quarantine manifest SHA-256 | `c1e41d4ccc2257c92162eb729d1d3dcc326a2742a1bff17087e1a1c11da8e175` |
| Build evidence | `NOT_OBSERVED_IN_SOURCE_CLONE`; the clean clone contains no local configuration or build-output markers |
| Effective execution scope | none; registry state `BLOCKED` |

The validator binds the exact owner acquisition grant, authorization-before-retrieval chronology, tag, Version.txt value, tagged CMake URLs, archive identities and inventories, quarantine bytes, and source-clone build markers. It also proves that all 24 model-tree files are byte-identical to the registered historical tree, that the tagged configuration selects the same 12 payload bytes, and that those payloads map exactly once to ten bundled license documents. This is bundle-internal documentary evidence only: it does not independently verify upstream rights, NOTICE obligations, or ISO/eu-LISA archive identity. Because no raw transfer log or response-header artifact was preserved, the HTTP/tool details remain project-recorded observations rather than independently verified transport evidence. No v1.1.2 build, installation, engine-version observation, conformance execution, or redistribution is claimed.

### Confirmed OFIQ v1.1.2 installed runtime (QS-A06.R2)

| Property | Confirmed value |
|---|---|
| Build root | `/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-build` (isolated sibling; `scripts/ofiq_v112_build.sh`, no sudo) |
| Source | fresh shallow clone of tag `v1.1.2`, commit `d589ca27896a472446abc9eca8b62947ee47db19`, cloned 2026-08-17T12:52:46-05:00 |
| Model bundle / ONNX Runtime archives | byte-identical to the preserved acquisition copies (`8ceb6cec…7f36`, `a0994512…e211`); hard-linked into `archives/` |
| Conformance-image archive | retrieved from eu-LISA during the build; SHA-256 `11aaebeb180810e0b2b42a5dafb356938c92ddf01592411d1d8f219988448399`, 374,153,032 bytes, response headers registered |
| Conan | 2.18.1 with a GCC 12, C++17, `libstdc++11`, `mode=report`, `sudo=False` profile; recipe sources fetched from ConanCenter (owner-authorized setup-time downloads) |
| Installed executable | `install/Release/bin/OFIQSampleApp` SHA-256 `e04864a5aa697e1297cdf78ba20d491d5fc68ecf5b3c595aa7bf0a3eb344d9e6` (RUNPATH `$ORIGIN`; no environment variable needed) |
| Installed library | `install/Release/bin/libofiq_lib.so` SHA-256 `d96f98b47f447c47c35f1d724daacd6d687545c638bb80694715e6f8c7f5e7c3` |
| ONNX Runtime library | `libonnxruntime.so.1.18.1` SHA-256 `1147caa734d19f7549f90a659aa9ca444366eeb80a596900d19d242145a9f4c9` (equal to the registered package member) |
| Configuration | `src/data/ofiq_config.jaxn` SHA-256 `e117286706d799a1e23130db01cfdff7ef36d157b22ecf68dd196052a8a8d0b3` (equal to the historical 1.1.0 configuration) |
| Model tree | 24 files; the 12-payload manifest (`fa908247…7ef7`) and 24-file glob manifest (`9d2ce532…3503`) are byte-identical to the registered historical and quarantine trees |
| Engine-reported version | `OFIQ library version: 1.1.2` |
| Official conformance table | `test_conformance_table` gtest binary, single process: 787 tests, 0 failures, 15.361 s (`conformance/gtest-conformance.json` SHA-256 `d94af4d0894c0454b1841009c3764a19a285f12ae21700ac28c5f1dec8e58a6d`) |
| r-01 smoke | UnifiedQualityScore.native 26.390884, scalar 79 (equal to the historical 1.1.0 observation); timing 2,394–2,707 ms initialization, 294–412 ms assessment across runs |
| Build receipt | `build-receipt.json` SHA-256 `31ecd59eb773863a021ace7101fff6eae0efa5bc8530f848f9e7e197a9208e32` |

The runtime is registered as `ofiq-v1-1-2-linux-runtime` (state `APPROVED_LOCAL_RESEARCH_USE`, third-party rights `VERIFIED_LICENSE` through the MIT engine/ONNX Runtime licenses and the ten bundled model license documents, scopes `REPRODUCE_OFIQ_CONFORMANCE_TESTS` and `QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1`). The historical 1.1.0 build-tree runtime stays `BLOCKED`/`NOT_APPROVED`, and the v1.1.2 acquisition candidate record stays as immutable acquisition evidence.

### Frozen three-matcher, two-preprocessing matrix

Both conditions begin with registered BGR `uint8` source images, the exact SCRFD-10G artifact with SHA-256 `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91`, detection threshold 0.5, and InsightFace five-point `norm_crop` to 112 × 112 pixels.

| Condition ID | Detector canvas | Face selection | Crop transport |
|---|---|---|---|
| `SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1` | 128 when the maximum source dimension is at most 160, 256 when it is at most 320, otherwise 640 | Largest bounding-box area | JPEG quality 95 followed by BGR decode |
| `SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1` | Fixed 640 × 640 | Highest detector confidence | In-memory BGR crop; no JPEG round trip |

| Matcher transform ID | Frozen transform |
|---|---|
| `ADAFACE_IR101_BGR_NEG1_POS1_L2_V1` | AdaFace IR-101; BGR CHW float32 scaled to [-1, 1]; finite 512-dimensional L2-normalized output |
| `ELASTICFACE_ARC_R100_RGB_NEG1_POS1_L2_V1` | ElasticFace-Arc R100; BGR-to-RGB CHW float32 scaled to [-1, 1]; finite 512-dimensional L2-normalized output |
| `TRANSFACE_S_P9_RGB_NEG1_POS1_L2_V1` | TransFace-S P9 depth 12; BGR-to-RGB CHW float32 scaled to [-1, 1]; finite 512-dimensional L2-normalized output |

QS-C22 refines to exactly six immutable leaves: `QS-C22.AS`, `QS-C22.AN`, `QS-C22.ES`, `QS-C22.EN`, `QS-C22.TS`, and `QS-C22.TN`, followed by one six-Run provenance join `QS-C22.R0`. Every leaf must preserve every official protocol pair, including explicit detection/template failures, and must emit one row per `(run_id, pair_id)` with no silent filtering. The source dataset manifest, ordered sample and subject IDs, pair manifest, score definition, SCRFD checkpoint, and five-point template stay fixed across all six Runs. A score exists only when both real templates succeed and must equal cosine similarity between finite 512-dimensional L2-normalized embeddings.

The historical shared-crop lineage text says fixed 640/highest-confidence, while the preserved extraction source uses adaptive canvas/largest-area selection. QS-C22.CROP.R0 now classifies the exact historical producer as adaptive/largest with a 112-pixel `norm_crop` call whose mode is not explicit in the generator and JPEG-quality-95 encoding. It does not map that producer to the full `ARCFACE112_JPEG95` condition because the ArcFace template and authorized BGR decode plus matcher consumption are not yet byte-bound. No matcher or SCRFD resource is execution-authorized, and no six-condition Run is claimed.

## Level 0 — Product graph interface

**Input:** canonical PRD, frozen constraints, approved real datasets and resources, current implementation state  
**Output:** a local-first QualSight beta that completes PRD section 57’s canonical workflow and satisfies section 58’s scientific traceability requirements

## Level 1 — Phase graph

~~~mermaid
flowchart LR
    P0[Phase 0: canonical foundation] --> P1[Phase 1: projects and quality]
    P1 --> P2[Phase 2: matcher evaluation]
    P2 --> P3[Phase 3: FIQA utility]
    P3 --> P4[Phase 4: cross-matcher T&E]
    P4 --> P5[Phase 5: visualization and reporting]
    P5 --> P7[Phase 7: hardening and beta gate]
    P7 --> BETA[Beta accepted]
    BETA -. explicit post-beta authorization .-> P6[Phase 6: advanced analysis]
~~~

P6 has no dependency into BETA.

## Level 2 — Capability DAG

~~~mermaid
flowchart TD
    A00[QS-A00 authority and requirement registry]
    A01[QS-A01 repository and toolchain]
    A02[QS-A02 resource, license, and real-data registry]
    A03[QS-A03 canonical schemas and immutable Run]
    A04[QS-A04 ExecutionAdapter contract]
    A05[QS-A05 Metric Registry]
    A06[QS-A06 OFIQ version reconciliation]
    A07[QS-A07 OFIQ CLI adapter]
    A08[QS-A08 API vertical slice]
    A09[QS-A09 PWA quality vertical slice]
    A10[QS-A10 Phase-0 reproducibility gate]

    A00 --> A01
    A00 --> A02
    A00 --> A03
    A00 --> A04
    A00 --> A05
    A01 --> A03
    A01 --> A04
    A01 --> A05
    A01 --> A06
    A02 --> A03
    A02 --> A04
    A02 --> A06
    A06 --> A03
    A06 --> A04
    A03 --> A07
    A04 --> A07
    A06 --> A07
    A07 --> A08
    A03 --> A08
    A05 --> A08
    A08 --> A09
    A02 --> A10
    A03 --> A10
    A05 --> A10
    A07 --> A10
    A09 --> A10

    A10 --> B10[QS-B10 project lifecycle]
    A10 --> B11[QS-B11 real dataset import]
    B10 --> B11
    B11 --> B12[QS-B12 batch quality jobs]
    A07 --> B12
    B12 --> B13[QS-B13 quality explorer and provenance]
    A09 --> B13
    B10 --> B14[QS-B14 Phase-1 gate]
    B11 --> B14
    B12 --> B14
    B13 --> B14

    B14 --> C20[QS-C20 comparison and protocol schemas]
    B14 --> C21[QS-C21 real score importer]
    A04 --> C21
    B14 --> C22[QS-C22 three matchers x two conditions]
    A04 --> C22
    C20 --> C22
    C20 --> C23[QS-C23 verification metrics]
    C22 --> C23
    A05 --> C23
    C23 --> C24[QS-C24 verification UI and failures]
    B13 --> C24
    C20 --> C25[QS-C25 Phase-2 gate]
    C22 --> C25
    C23 --> C25
    C24 --> C25

    C25 --> D30[QS-D30 pair-quality policies]
    D30 --> D31[QS-D31 ERC and risk-coverage]
    C23 --> D31
    A05 --> D31
    D31 --> D32[QS-D32 uncertainty and error models]
    D31 --> D33[QS-D33 threshold-policy analysis]
    D32 --> D34[QS-D34 Phase-3 gate]
    D33 --> D34

    D34 --> E40[QS-E40 compatible selected matcher set]
    C22 --> E40
    E40 --> E41[QS-E41 cross-matcher metrics]
    D32 --> E41
    E41 --> E42[QS-E42 failure-analysis workbench]
    C24 --> E42
    E41 --> E43[QS-E43 Phase-4 gate]
    E42 --> E43

    E43 --> F50[QS-F50 provenance-bearing figures]
    F50 --> F51[QS-F51 dashboards]
    F50 --> F52[QS-F52 reports and exports]
    A03 --> F53[QS-F53 reproduction bundle and mode]
    E43 --> F53
    F51 --> F54[QS-F54 Phase-5 gate]
    F52 --> F54
    F53 --> F54

    F54 --> H70[QS-H70 security and threat model]
    F54 --> H71[QS-H71 single-user attribution and audit]
    F54 --> H72[QS-H72 offline packaging]
    F54 --> H73[QS-H73 operating documentation]
    H70 --> H74[QS-H74 beta end-to-end gate]
    H71 --> H74
    H72 --> H74
    H73 --> H74
    C21 --> H74
~~~

The coarse beta capability graph contains 41 nodes and 86 dependency edges and is acyclic. QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, and QS-D31.R0 are current evidence-bearing children used to refine their parent interfaces; superseded resource, domain, and execution records remain in the append-only ledger. Refinement children and their refinement parents are outside the coarse count and cannot satisfy or bypass a parent dependency.

## Capability registry

Statuses in this table are graph states, not claims of implementation.

| ID | PRD source | Output interface | Acceptance predicate | Dependencies | State |
|---|---|---|---|---|---|
| QS-A00 | §§5, 55–58 | Versioned beta requirement registry with stable IDs and source links | Every beta clause maps to one required, conditional, deferred, or post-beta status and one acceptance owner | none | ACCEPTED |
| QS-A01 | §§45–48, 56 Phase 0 | Initialized local repository and pinned project toolchain | Git root is /mnt/projects/ofiqgui; dependency manifests exist; no generated sample application or incomplete execution path exists | QS-A00 | ACCEPTED |
| QS-A02 | §§10, 48, 52 | Resource/license/data registry | Every consumed file has identity, version, source, use restriction, hash, and access method; project-owner authorization, third-party-rights evidence, and effective approved scope are separate and complete | QS-A00 | ACCEPTED |
| QS-A03 | §§7–9, 41–42, 47, 50 | Canonical project, dataset, sample, experiment, Run, artifact, and audit schemas | Schema and immutable-Run tests pass; historical preservation gaps are recorded without relabeling and prohibit live reproduction; parent acceptance requires a new authorized Run with complete contemporaneous provenance | QS-A00, QS-A01, QS-A02, QS-A06 | ACCEPTED |
| QS-A04 | §51 | Typed ExecutionAdapter contract and lifecycle | Contract tests prove schema validation, version/provenance capture, failure propagation, cancellation, process-group termination, and artifact identity using authorized registered resources | QS-A00, QS-A01, QS-A02, QS-A06 | ACCEPTED |
| QS-A05 | §§15–32, 43, 56 Phase 0 | Machine-readable Metric Registry | Registry schema validates and contains versioned FMR, FNMR, ROC, DET, EER, and ERC definitions before metric UI code exists | QS-A00, QS-A01 | ACCEPTED |
| QS-A06 | §§51–54; PLAN §4.2 | Verified OFIQ v1.1.2 resource record and integration decision | Rights close; a no-sudo isolated build consumes the preserved tagged-CMake archives; installed runtime closure is pinned; the engine reports 1.1.2 and passes real conformance validation without relabeling v1.1.0 | QS-A01, QS-A02 | ACCEPTED |
| QS-A07 | §§13–14, 51, 53 | Real OFIQ CLI ExecutionAdapter | Adapter and direct CLI agree on all non-timing fields for the registered conformance image; failures remain explicit; provenance is complete | QS-A03, QS-A04, QS-A06 | ACCEPTED |
| QS-A08 | §§9, 44–46 | FastAPI endpoint executing QS-A07 and persisting an immutable Run | Real image request returns typed quality result and Run ID; source input is not copied or transmitted; API and persisted result agree | QS-A03, QS-A05, QS-A07 | ACCEPTED |
| QS-A09 | §§14, 35–36, 44, 46 | React/TypeScript PWA sample inspector | Browser renders the real API result, distinguishes engine measurement from policy, and exposes run/provenance links | QS-A08 | ACCEPTED |
| QS-A10 | §56 Phase 0 | Reproducible Phase-0 evaluation | A clean run reproduces the registered result from exact input/resource hashes and exports a complete immutable Run manifest | QS-A02, QS-A03, QS-A05, QS-A07, QS-A09 | ACCEPTED |
| QS-B10 | §§7–9 | Project lifecycle and persistence | Create, save, close, reopen, clone, and archive preserve exact scientific configuration and immutable Run references | QS-A10 | ACCEPTED |
| QS-B11 | §§10–12 | Real dataset import and manifest validation | Approved real dataset imports with identity mapping, hashes, license, permissions, completeness counts, and deterministic real-data subset fixture | QS-A10, QS-B10 | ACCEPTED |
| QS-B12 | §§13–14, 32 | Batch quality execution | Every registered real fixture image yields a result or explicit assessment failure; progress, cancellation, resource use, and provenance are recorded | QS-A07, QS-B11 | ACCEPTED |
| QS-B13 | §§11, 14, 33–36 | Quality explorer and drilldown | Dataset distributions reconcile to batch rows; selecting a real point resolves to its exact sample, Run, and provenance | QS-A09, QS-B12 | ACCEPTED |
| QS-B14 | §56 Phase 1 | Phase-1 accepted artifact | Project lifecycle, real import, single and batch quality, visualization, and provenance pass one integrated real-data workflow | QS-B10, QS-B11, QS-B12, QS-B13 | ACCEPTED |
| QS-C20 | §§15–18, 21, 24 | Verification comparison and protocol schemas | Mated/non-mated labels, pair policy, thresholds, processing failures, and 1:N-compatible entities validate without conflation | QS-B14 | ACCEPTED |
| QS-C21 | §§12, 51.8, 54 Tier 1 | Real external score importer | A registered real score artifact maps identifiers and labels without row invention and remains marked EXTERNALLY COMPUTED | QS-A04, QS-B14 | BLOCKED_BY_REAL_SCORES_PARKED_TO_QS-H74_BY_DECISION_28 |
| QS-C22 | §§51, 53–54 | Three selected matchers × two preprocessing conditions | AdaFace, ElasticFace, and TransFace each produce separate shared/native immutable Runs over one frozen real protocol; every pair and explicit failure is retained; automatic downloads, random weights, unsafe loads, and unregistered bytes are refused | QS-A04, QS-B14, QS-C20 | ACCEPTED |
| QS-C23 | §§15–16, 23–24, 28–30 | Verification metric engine | Each of the six result sets reconciles to independently checked calculations over the same real comparisons and reports counts, uncertainty, thresholds, failures, and limitations | QS-A05, QS-C20, QS-C22 | ACCEPTED |
| QS-C24 | §§24, 33, 35–36 | Verification UI and failure drilldown | The UI displays both preprocessing conditions for all three matchers; every real error links to its comparison, samples, subjects, threshold, condition, transform, and immutable Run | QS-B13, QS-C23 | ACCEPTED |
| QS-C25 | §56 Phase 2 | Complete 1:1 evaluation | One frozen real protocol produces six condition-specific QualSight-executed Runs, score distributions, FMR/FNMR, ROC, DET, EER, operating points, uncertainty, and traceable failure cases (imported-score evidence moved to QS-C21 → QS-H74 by decision 28) | QS-C20, QS-C22, QS-C23, QS-C24 | ACCEPTED |
| QS-D30 | §21 | Pair-quality policy registry | Minimum, maximum, mean, geometric mean, enrollment, and probe policies are versioned and produce traceable values from real pairs | QS-C25 | ACCEPTED |
| QS-D31 | §20 | ERC, AU-ERC, partial AU-ERC, and risk-coverage engine | Rejection domain, retained coverage, fixed matcher threshold policy, pair aggregation, counts, and uncertainty are recorded and independently checked | QS-A05, QS-C23, QS-D30 | ACCEPTED |
| QS-D32 | §§16, 20, 28 | Uncertainty and quality-error models | Subject-aware resampling and supported models run only when the real sample supports them; unsupported precision is flagged | QS-D31 | ACCEPTED |
| QS-D33 | §24 | Threshold-policy analysis | Real acceptance/rejection and recognition impacts reconcile exactly to source comparisons at each chosen policy threshold | QS-D31 | ACCEPTED |
| QS-D34 | §56 Phase 3 | Quality-to-utility accepted artifact | Real quality scores and comparisons yield traceable ERC, risk-coverage, uncertainty, error modeling, and policy analysis | QS-D32, QS-D33 | ACCEPTED |
| QS-E40 | §§22, 53–54 | Compatible selected matcher result set | At least two of AdaFace, ElasticFace, and TransFace have rights-cleared Runs over the same real protocol and the same preprocessing condition; preprocessing effects remain a separate within-matcher contrast | QS-C22, QS-D34 | DEPENDENCY_READY |
| QS-E41 | §22 | Cross-matcher and preprocessing metric engine | Matcher contrasts hold preprocessing fixed; preprocessing contrasts hold matcher and protocol fixed; ERC overlays, differences, error overlap, interactions, and paired uncertainty preserve both identities | QS-D32, QS-E40 | BLOCKED_BY_PREDECESSORS |
| QS-E42 | §§22, 33–34 | Cross-matcher failure workbench | Every disagreement resolves to real comparisons, permitted images, quality values, thresholds, matcher provenance, preprocessing provenance, and annotations | QS-C24, QS-E41 | BLOCKED_BY_PREDECESSORS |
| QS-E43 | §56 Phase 4 | Cross-matcher accepted artifact | The selected matcher set completes fixed-condition matcher comparisons and fixed-matcher preprocessing comparisons with traceable utility differences and failure evidence | QS-E41, QS-E42 | BLOCKED_BY_PREDECESSORS |
| QS-F50 | §§35–36, 40 | Provenance-bearing figure artifacts | PNG, SVG, and PDF outputs link to source rows, Run, metric definition, thresholds, configuration, and exportable values | QS-E43 | BLOCKED_BY_QS-E43 |
| QS-F51 | §37 | Saved dashboards | Saved layout reloads exactly and every panel retains the underlying Run and provenance references | QS-F50 | BLOCKED_BY_QS-F50 |
| QS-F52 | §§38–40 | Reports and exports | PDF, HTML, print, CSV, JSON, Parquet, SVG, PNG, and PDF outputs enforce data classification and reconcile to source results | QS-F50 | BLOCKED_BY_QS-F50 |
| QS-F53 | §§41–42 | Reproduction bundle and mode | Bundle checksums validate; restricted images are excluded; rerun status is EXACT, WITHIN_TOLERANCE, DIFFERENT, MISSING_DEPENDENCY, MISSING_DATA, or BLOCKED based on observed evidence | QS-A03, QS-E43 | BLOCKED_BY_PREDECESSORS |
| QS-F54 | §56 Phase 5 | Reporting accepted artifact | Real cross-matcher Run produces dashboard, report, figures, tabular exports, and a validated reproduction bundle | QS-F51, QS-F52, QS-F53 | BLOCKED_BY_PREDECESSORS |
| QS-H70 | §§48–50, 56 Phase 7 | Threat model and verified local data controls | No runtime cloud/image transmission; secrets absent from logs; purge scope is tested and described as application-level verified purge | QS-F54 | BLOCKED_BY_QS-F54 |
| QS-H71 | §§6, 50, 55 | Single-user attribution and audit | Every meaningful scientific action records configured user and timestamp; optional local-account mode is not required for this gate | QS-F54 | BLOCKED_BY_QS-F54 |
| QS-H72 | §§46–48, 56 Phase 7 | Offline installation and packaging | Installation on the controlled workstation succeeds from pinned resources and the product runs with network disabled after installation | QS-F54 | BLOCKED_BY_QS-F54 |
| QS-H73 | §§38, 56 Phase 7 | Operating and scientific documentation | A researcher completes the canonical workflow using only shipped documentation and real registered resources | QS-F54 | BLOCKED_BY_QS-F54 |
| QS-H74 | §§55, 57–60 | Beta end-to-end acceptance | The complete section-57 workflow runs without manual Python analysis and every reportable result satisfies sections 58 and 60 | QS-C21, QS-H70, QS-H71, QS-H72, QS-H73 | BLOCKED_BY_PREDECESSORS |

## Post-beta branch

These nodes have no edge into QS-H74:

| ID | PRD source | Capability | State |
|---|---|---|---|
| QS-X59 | §56 Phase 6 | Advanced-analysis release gate | POST_BETA |
| QS-X60 | §17, Phase 6 | 1:N identification execution and metrics | POST_BETA |
| QS-X61 | §27, Phase 6 | Controlled transformations of approved real inputs with complete derived-data provenance | POST_BETA |
| QS-X62 | §25, Phase 6 | Demographic analysis with approved data, metadata, research authorization, and uncertainty | POST_BETA |
| QS-X63 | §26, Phase 6 | PAD adapter and evaluation | POST_BETA |
| QS-X64 | §51.9 | Container and REST adapters | POST_BETA |
| QS-X65 | §§5, 46 | Mobile, kiosk, and remote capture clients | POST_BETA |
| QS-X66 | §§5–6 | Enterprise SSO/OIDC and hosted multi-user identity | POST_BETA |
| QS-X67 | §§5, 54 Tier 4 | Commercial matcher SDK adapters | POST_BETA |
| QS-X68 | §§10, 51.11 | Non-face modality execution | POST_BETA |

QS-X61 never authorizes fabricated identities, labels, scores, or source samples. Any derived transformation must begin from a registered real input and preserve its full transformation chain.

## Current atomic frontier

Phases 0, 1, 2, and 3 are complete (Phase 2 under owner decision 28): whole capabilities QS-A00 through QS-A10, QS-B10 through QS-B14, QS-C20, QS-C22, QS-C23, QS-C24, QS-C25, and QS-D30 through QS-D34 are ACCEPTED. Current accepted evidence children are QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, and QS-D31.R0. The ledger preserves superseded resource, domain, and execution events as history, but their stale registry and fixture-byte claims are not reused as current evidence. Gates marked *executing* launch the registered installed OFIQ v1.1.2 runtime on registered inputs under the owner use-permission receipt; every other gate is static. Every Node command below assumes `PATH=/usr/bin:$PATH` so that Node 20.20.2 is selected.

### QS-A00 — Authority and requirement registry

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_atg_documents.py
~~~

**Acceptance:** the PRD, PLAN, and owner-authorization identities match the frozen source snapshot; REQUIREMENTS.md v0.2 contains 81 unique requirement records covering all 60 PRD sections and 12 decision/source rows; the capability graph contains 41 nodes and 86 acyclic edges; every current acceptance and supersession claim resolves to the append-only ledger.  
**State:** ACCEPTED through refinement QS-A00.R8.

### QS-A01 — Repository and pinned toolchain

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv lock --check
/home/aaron/.local/bin/uv run --frozen --offline python -c "import platform; assert platform.python_version() == '3.11.2'"
pnpm install --offline --frozen-lockfile --ignore-scripts
pnpm run verify:toolchain
~~~

**Acceptance:** uv resolves the two real workspace members from the frozen lock under Python 3.11.2 and uv 0.9.26; pnpm reproduces its dependency-empty workspace lock under Node 20.20.2 and pnpm 10.28.0; caches remain project-local.  
**State:** ACCEPTED. The Python lock now carries the justified QS-A03 Pydantic set plus the QS-A08 FastAPI/uvicorn/httpx set; the pnpm lock carries the QS-A09 console set; `pnpm run verify:toolchain` and `uv lock --check` pass under Node 20.20.2 / pnpm 10.28.0 / uv 0.9.26.

### QS-A05 — Versioned Metric Registry

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_metric_registry.py metrics/metric-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_metric_registry tests.test_resource_registry
~~~

**Acceptance:** the validator reports registry `qualsight-core-metrics` version 1.0.1 with six unchanged definition-version 1.0.0 records—FMR, FNMR, ROC, DET, EER, and ERC—two byte-identified local authorities, and nine requirement links; all eight metric tests and all 20 resource tests pass. Definitions remain `DEFINITION_ONLY`, and no score or metric computation is claimed.  
**State:** ACCEPTED through authority-only refinement QS-A05.R1.

### QS-A06.R0 — Official v1.1.2 acquisition-evidence slice

**Safe gate**

~~~sh
/usr/bin/git -C /mnt/projects/02_perception_biometrics/OFIQ-Project-v1.1.2 status --porcelain=v1 --untracked-files=all --ignored=matching
sha256sum /mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/acquisition-receipt.json /mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/OFIQ-Models.zip /mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts/onnxruntime-linux-x64-1.18.1.tgz
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
~~~

**Acceptance:** the clean official v1.1.2 source tag, exact tagged-CMake URL selection, authorization chronology, two raw archive identities, safe archive inventories, exact quarantine trees, and lack of source-clone build markers validate. Missing raw transfer logs remain explicit.  
**Historical state:** ACCEPTED as an acquisition-evidence child in cycle C05, then SUPERSEDED by QS-A06.R1M for current-byte reuse after that child revalidated every R0 predicate. Its acquisition evidence remains immutable ledger history.

### QS-A06.R1M — Static v1.1.2 model-bundle equivalence and license map

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry.ResourceRegistryTests.test_ofiq_v112_quarantine_model_tree_is_exact_registered_byte_equivalent tests.test_resource_registry.ResourceRegistryTests.test_ofiq_v112_quarantine_license_map_covers_each_registered_model_payload tests.test_resource_registry.ResourceRegistryTests.test_ofiq_v112_model_evidence_does_not_promote_or_authorize_execution
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 16 real-registry tests pass. The validator revalidates every QS-A06.R0 acquisition predicate, binds the receipt-selected ZIP quarantine, and proves a 24-file root-normalized manifest with 12 MODEL, ten LICENSE, and two DOCUMENTATION artifacts. Every file is byte-identical to the registered historical model tree; the tagged v1.1.2 configuration closes over exactly the same 12 payloads; and ten exact bundled documents cover those payloads once each.  
**Historical state:** ACCEPTED as bundle-internal static evidence in cycle C07, then SUPERSEDED by QS-A06.R1O for current-byte reuse after that child revalidated every R1M predicate. Its exact 24-file and ten-document result remains immutable ledger history.

### QS-A06.R1O — Authorized ONNX Runtime static package and ELF preflight

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry.ResourceRegistryTests.test_ofiq_v112_onnxruntime_archive_registers_every_static_evidence_byte tests.test_resource_registry.ResourceRegistryTests.test_ofiq_v112_onnxruntime_elf_preflight_is_static_and_non_promoting
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 18 real-resource tests pass. The validator revalidates every QS-A06.R1M predicate, binds the separately timestamped static-review authorization before reading governed ONNX bytes, the receipt-selected TAR_GZIP quarantine, all 19 regular files, the exact three-directory and one-symlink layout, and the tagged Linux `USE_CONAN` import/install configuration. It proves package version 1.18.1, package-recorded commit `387127404e6c1d84b3468c387d864877ed1c67fe`, API version 18, the exact MIT and third-party notice bytes, and static ELF declarations including SONAME, `$ORIGIN` RUNPATH, eight ordered dependencies, two required `VERS_1.18.1` exports, exact version-needs edges, and the GNU build ID.  
**State:** ACCEPTED as non-executing package and static-ELF evidence. It proved nothing about loadability, build, installation, engine version, or conformance; those predicates were closed by QS-A06.R2 below, and the acquisition candidate record itself stays as immutable evidence.

### QS-A02.R1 — Expanded resource and rights-boundary slice

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 13 tests pass; the validator reports eight resources, 80 byte-identified artifacts, 10 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. Owner grants are resource-specific and separate from third-party rights; only r-01 has an effective limited-use scope. The full real LFW image tree has a 13,233-file byte manifest.  
**Historical state:** ACCEPTED in cycle C05, then SUPERSEDED by QS-A02.R2 for current-byte reuse. Its exact 80-artifact result remains immutable ledger history.

### QS-A02.R2 — Historical 81-artifact registry revalidation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 13 resource tests pass; the validator reports eight resources, 81 byte-identified artifacts, 10 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. The new protocol artifact is present without weakening any resource state, rights status, or scope boundary.  
**Historical state:** ACCEPTED as the registry-byte child in cycle C06, then SUPERSEDED by QS-A02.R3 after the v1.1.2 model quarantine, license-document map, and tagged configuration closure changed the registry identity. Its exact result remains immutable ledger history.

### QS-A02.R3 — Historical 106-artifact registry revalidation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 16 resource tests pass; the validator reports eight resources, 106 byte-identified artifacts, 12 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. The v1.1.2 candidate adds the exact tagged configuration and all 24 quarantine model-tree files without changing any resource state, third-party-rights status, owner grant, effective scope, or redistribution boundary.  
**Historical state:** ACCEPTED as the registry-byte child in cycle C07, then SUPERSEDED by QS-A02.R4 after the separate ONNX static-review receipt, 19-file package registration, tagged Linux configuration, and static preflight changed the registry identity. Its exact result remains immutable ledger history.

### QS-A02.R4 — Historical 127-artifact registry revalidation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry
~~~

**Acceptance:** all 18 resource tests pass; the validator reports schema 1.2.0, eight resources, 127 byte-identified artifacts, 13 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. The v1.1.2 candidate adds one exact static-review authorization, the tagged Linux configuration, and all 19 receipt-bound ONNX Runtime package files without changing any resource state, third-party-rights status, effective approved scope, or redistribution boundary.  
**Historical state:** ACCEPTED as the registry-byte child in cycle C08, restored after the first incomplete R5 attempt regressed three exact-registry consumers, then SUPERSEDED only after the corrected R5 subgraph revalidated all consumers. Its exact results and the failed attempt remain immutable ledger history.

### QS-A02.R5 — Historical 132-artifact registry revalidation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry tests.test_scrfd_release_provenance tests.test_lfw_protocol_authority tests.test_shared_crop_lineage
~~~

**Acceptance:** all 25 affected real-byte tests pass: 19 resource, two SCRFD provenance, two LFW protocol-authority, and two shared-crop lineage tests. The validators report schema 1.2.0, eight resources, 132 byte-identified artifacts, 13 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. The SCRFD record adds the exact evidence-only authorization, official InsightFace v0.7 release API bytes, API and final-asset response headers, and the preserved `buffalo_l.zip`; the current archive and registered `det_10g.onnx` are byte-bound to the official release. Every accepted exact-registry consumer is revalidated without changing any resource state, third-party-rights status, effective approved scope, or redistribution boundary.  
**Historical state:** ACCEPTED as the registry-byte child and replacement for QS-A02.R4, then SUPERSEDED by QS-A02.R6 after the accepted official LFW protocol evidence was registered in its dataset record. Parent QS-A02 remained BLOCKED.

### QS-A02.R6 — Current 140-artifact registry revalidation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry tests.test_scrfd_release_provenance tests.test_lfw_protocol_authority tests.test_shared_crop_lineage
~~~

**Acceptance:** all 26 affected real-byte tests pass: 20 resource, two SCRFD provenance, two LFW protocol-authority, and two shared-crop lineage tests. The validators report schema 1.2.0, eight resources, 140 byte-identified artifacts, 13 non-executing checks, seven BLOCKED resources, and one VERIFIED_LIMITED_USE resource. The LFW record adds the two exact evidence-only authorizations plus three official UMass PDFs and their response-header bytes, and replaces its stale missing-protocol blocker with the remaining matcher-training-identity and runtime/effective-scope blockers. Every accepted exact-registry consumer is revalidated without changing any resource state, third-party-rights status, effective approved scope, or redistribution boundary.  
**Historical state:** ACCEPTED as the registry-byte child in cycle C14, then SUPERSEDED by QS-A02.R7 after the owner use-permission receipt, the five `APPROVED_LOCAL_RESEARCH_USE` transitions, and the installed-runtime registration changed the registry identity.

### QS-A02.R7 — Owner use-permission, effective scopes, and installed-runtime registration

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_resource_registry tests.test_scrfd_release_provenance tests.test_lfw_protocol_authority tests.test_shared_crop_lineage tests.test_lfw_protocol
/home/aaron/.local/bin/uv run --frozen --offline python scripts/repin_registry_identity.py
~~~

**Acceptance:** all 28 affected real-byte tests pass; the validator reports schema 1.2.0, nine resources, 187 byte-identified artifacts, 17 non-executing checks, six `APPROVED_LOCAL_RESEARCH_USE` resources, two `BLOCKED` resources (the 1.1.0 build-tree runtime and the 1.1.2 acquisition candidate), and one `VERIFIED_LIMITED_USE` resource. The receipt `qualsight-owner-use-permission-granted-2026-08-17.json` (recorded 2026-08-17T12:50:33-05:00, before any build or execution) is registered on every promoted resource; each promoted resource keeps a factual rights status (`NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET`, `REPOSITORY_LICENSE_ONLY`, `VERIFIED_LICENSE`, `NO_LICENSE_FOUND`, `RESEARCH_NOTICE_ONLY`), an empty blocker list, an installation method that names the setup-time pinned acquisition source, and effective scopes inside the owner grant. The new `ofiq-v1-1-2-linux-runtime` record binds 42 artifacts including the installed executable, both loaded libraries, the configuration, all 24 model-tree files, the three archives with response headers, the Conan profile, the build receipt, and the conformance JSON/log, with the same four executable checks as the historical runtime (git identity, 12-payload manifest, 24-file glob, jaxn closure). `repin_registry_identity.py` moves the exact registry identity into the three evidence manifests and their validators, and all four consumer validators re-pass.  
**State:** ACCEPTED as the current registry-byte child and replacement for QS-A02.R6. Parent QS-A02 is ACCEPTED: every consumed file has identity, version, source, use restriction, hash, and access method, and owner authorization, third-party rights, and effective scope are recorded separately and completely.

### QS-A02.LFW.R0 — LFW local-byte and pair-format reconciliation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_lfw_protocol.py verify --registry resources/resource-registry.json --manifest resources/manifests/lfw-local-pairs-v1.jsonl
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_lfw_protocol tests.test_resource_registry
~~~

**Acceptance:** the validator stream-checks all 18,994 members of the registered real LFW funneled archive without decoding an image; the 13,233 archive JPEGs in 5,749 identity directories and 11 protocol files match the extracted tree bytes; root, archive, and extracted `pairs.txt` are identical; all ten expanded fold files match the source semantics; and the deterministic manifest preserves all 3,000 mated and 3,000 non-mated pairs, source order, labels, paths, sizes, and SHA-256 identities across 7,701 unique samples and 4,281 subjects. The C06 acceptance gate passed 15 focused tests; the current v1.5 combined protocol-and-resource gate (`tests.test_lfw_protocol tests.test_resource_registry`) passes 22.  
**State:** ACCEPTED as local byte-and-format evidence only. Its four explicit limits are `NOT_AN_ACQUISITION_RECEIPT`, `NOT_LICENSE_OR_PERMISSION_EVIDENCE`, `NOT_COMPLETE_OFFICIAL_PROTOCOL_CONFORMANCE`, and `NO_IMAGE_DECODING_OR_MODEL_EXECUTION`. Refinement parent QS-A02.LFW and capability parent QS-A02 are ACCEPTED under QS-A02.R7; the dataset now carries the factual rights status `NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET` and the owner-granted local research scopes.

### QS-A02.LFW.P0 — Official LFW protocol-authority reconciliation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_lfw_protocol_authority.py
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_lfw_protocol_authority
~~~

**Acceptance:** both real-document tests pass. The validator checks two timestamped project-owner evidence-acquisition receipts before reading governed PDF bytes; pins the local curl and pdftotext identities; binds three exact official UMass PDFs and response-header records; reproduces the original View 2 definition of 6,000 pairs in ten folds; reconciles that definition to the exact registered 6,000-row local pair manifest; and classifies the planned pretrained-matcher study conservatively as `UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA`. The preserved URLs are project-recorded because verbose request traces were not retained.  
**State:** ACCEPTED as non-executing protocol-authority evidence only. The child does not prove LFW image rights, historical dataset acquisition, exclusion of every LFW test identity from every matcher training set, complete official-protocol conformance, a common effective scope, or matcher execution. The protocol finding is now `OWNER_AUTHORIZED_LOCAL_RESEARCH_USE_MATCHER_TRAINING_EXCLUSION_RECORDED_AS_PUBLISHED_CLAIM`; refinement parent QS-A02.LFW and capability parent QS-A02 are ACCEPTED under QS-A02.R7; no LFW image was decoded or displayed by this child.

### QS-A02.SCRFD.P0 — InsightFace v0.7 buffalo_l release-provenance reconciliation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_scrfd_release_provenance.py
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_scrfd_release_provenance
~~~

**Acceptance:** both real-byte tests pass. The validator checks the timestamped project-owner evidence-acquisition receipt before governed archive access; pins the local curl identity and preserved GitHub release API and response-header bytes; binds official tag `v0.7`, asset ID `101975360`, the 288,621,354-byte `buffalo_l.zip`, SHA-256 `80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f`, and MD5 `6c0e929fd3b6ab517170b732ced18c68`; validates the exact five regular unencrypted ZIP members; and stream-compares `det_10g.onnx` to the registered 16,923,827-byte detector. The fresh authorized transfer matched the preserved archive byte-for-byte and was removed after the evidence records were published.  
**State:** ACCEPTED as non-executing current-release provenance evidence only. The child does not reconstruct the existing archive's original acquisition event, establish redistribution rights, approve an effective scope, validate a compatible frozen runtime, or authorize model execution. SCRFD is `APPROVED_LOCAL_RESEARCH_USE` only through the owner receipt bound by QS-A02.R7 (rights status stays `RESEARCH_NOTICE_ONLY`); no ZIP member was extracted and no detector or matcher was executed by this child.

### QS-A03.R1 — Historical-preservation repair

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.domain.test_entities tests.domain.test_run tests.domain.test_real_ofiq_fixture
~~~

**Acceptance:** all 16 domain tests pass against fixture schema 1.2.0. Twenty preserved byte identities are current; the overwritten historical registry bytes and historical capture-tool bytes are explicitly `NOT_RECORDED`; live reproduction refuses before execution; the restricted image remains external and uncopied.  
**State:** ACCEPTED as a historical-preservation child. The recorded `uv.lock` identity is historical environment evidence (the live lock gained the QS-A08 dependencies on 2026-08-17) and the fixture's verification-tool identity was re-recorded when `tests/real_fixture.py` learned to report that. Parent QS-A03 is ACCEPTED through the new authorized Runs of QS-A07/QS-A08/QS-A10, whose provenance is contemporaneous.

### QS-A04.R1 — Current-registry contract revalidation

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.test_execution_contract tests.test_execution_cli_real_ofiq
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -t . -v
~~~

**Acceptance:** all 13 focused tests and all 29 backend tests pass without starting OFIQ. They prove the generic typed adapter/handle surface, deep immutability, exact current-registry membership for every operational projection, strict registered executable/config/input paths, one common approved scope on every consumed resource, and refusal of the BLOCKED runtime before `Popen`.  
**State:** ACCEPTED as a current-registry safety child; the refusal test now targets the still-BLOCKED historical 1.1.0 runtime and the adapter accepts the states `VERIFIED_LIMITED_USE` and `APPROVED_LOCAL_RESEARCH_USE`. Parent QS-A04 is ACCEPTED through QS-A04.R2.

### QS-C22.CROP.R0 — Historical shared-crop producer reconciliation

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_shared_crop_lineage.py resources/manifests/lfw-full-v1-shared-crop-lineage-reconciliation.json
/home/aaron/.local/bin/uv run --frozen --offline python -m unittest -v tests.test_shared_crop_lineage
~~~

**Acceptance:** both real-byte tests pass. The validator binds the exact historical manifest, generator source, sealed SCRFD implementation, current resource registry, owner decision, and 16,923,827-byte detector model. It proves that the historical and registered detector identities are equal and classifies only the preserved producer behavior: adaptive 128/256/640 sizing, largest-area selection, one 112-pixel `norm_crop` call with no explicit mode argument, and JPEG-quality-95 encoding.  
**State:** ACCEPTED as non-executing lineage-classification evidence (revalidated as QS-C22.CROP.R0.V2). The mutable generator path was rewritten outside QualSight at 2026-08-17T07:23:09-05:00 (same 31,363 bytes, new digest); the reconciliation now retains the historical digest `6a44a96e…c77c`, binds the current bytes separately, and re-derives the same classification from the current AST. The historical producer is not mapped onto the frozen condition; instead QS-C22.MATRIX.LFW executes `SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1` afresh with a byte-bound ArcFace template, in-memory BGR decode, and registered matcher consumption, so this child stays historical-classification evidence only.

### QS-A06.R2 — Isolated no-sudo v1.1.2 build, installation, and conformance (executing)

**Safe gate**

~~~sh
bash scripts/ofiq_v112_build.sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_resource_registry.py resources/resource-registry.json
~~~

**Acceptance:** the setup script clones the official tag at the pinned commit, hard-links the byte-identical preserved model and ONNX Runtime archives, retrieves and hashes the conformance-image archive, installs Conan 2.18.1 as a user tool, resolves dependencies with a GCC 12 no-sudo profile, builds and installs, runs the r-01 smoke (`OFIQ library version: 1.1.2`, UQS 26.390884/79), and writes `build-receipt.json`; the gtest conformance binary reports 787/787 passed; the registry validator proves the installed runtime record and the byte-identical model manifests.  
**State:** ACCEPTED. Parent QS-A06 is ACCEPTED: rights are recorded (MIT engine and ONNX Runtime, ten bundled model licenses), the build consumed the preserved archives, the installed closure is pinned, the engine reports 1.1.2, and the official conformance table passes.

### QS-A04.R2 — Real lifecycle evidence on the registered v1.1.2 runtime (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.test_execution_cli_real_ofiq_v112 tests.test_execution_contract tests.test_execution_cli_real_ofiq
~~~

**Acceptance:** five real launches of the registered binary on the registered r-01 input prove success collection with lifecycle CREATED→PREPARED→RUNNING→SUCCEEDED and stdout capture, cancellation during initialization with SIGTERM process-group termination and a CANCELLED record, no-op cancellation after completion, refusal of a purpose scope outside the input grant before launch, and refusal of the BLOCKED historical runtime; `tests/engines/test_ofiq_engine_real.py` adds a real nonzero-exit process of the same binary collected through the same `RunningExecution` path (FAILED, exit 1, `NONZERO_EXIT`).  
**State:** ACCEPTED. Parent QS-A04 is ACCEPTED.

### QS-A07 — Registry-bound OFIQ quality engine (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.engines.test_ofiq_engine_real
~~~

**Acceptance:** `qualsight.engines.ofiq` parses the engine's own output into 28 ISO/IEC 29794-5 measures with native and scalar values, keeps engine failure sentinels and unreadable-input rows as explicit failures, and builds an immutable `CompletedRun` (56 metric observations, 12 model hashes, contemporaneous repository/environment/hardware provenance) whose JSON round trip is identity; repeated assessment reproduces every non-timing value; the registered LFW archive (a real non-image DATA artifact) yields an explicit row failure and no Run; unregistered paths and non-common scopes are refused before launch.  
**State:** ACCEPTED (7/7 real tests).

### QS-A08 — FastAPI control plane and immutable SQLite Run store (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_quality_api_real
~~~

**Acceptance:** `POST /api/quality/assess` on the registered r-01 image executes the registered engine, persists project/dataset/sample/experiment/Run/audit records append-only in SQLite (context entities first-write-wins by content-addressed ID, Runs and audit events byte-exact), and returns the typed `ENGINE_MEASUREMENT` result with `policy: NONE_APPLIED`; `GET /api/runs/{run_id}` re-reads the stored bytes and agrees metric-for-metric with the response and with the SQLite evidence digest; provenance links resolve; scope refusal is 403, unregistered path 404, unreadable registered input 422 `ASSESSMENT_FAILED`; no image byte is copied into the data directory.  
**State:** ACCEPTED (6/6 real tests).

### QS-A09 — React/TypeScript PWA sample inspector (executing, visual)

**Safe gate**

~~~sh
PATH=/usr/bin:$PATH pnpm --filter @qualsight/frontend build
python3 scripts/observe_console.py --screenshot-dir <outside the repository> --observation atg/evidence/2026-08-17-console-observation-01.json
~~~

**Acceptance:** the Vite build type-checks and emits the PWA (manifest, service worker that never caches `/api`); the observation script starts the control plane and `vite preview`, drives headless Chromium 145.0.7632.6, and records that the console loads with its manifest, shows the registered engine badge, renders the registered image, executes one real assessment from the browser, displays 28 measure rows under an `ENGINE MEASUREMENT · policy: none applied` banner with UnifiedQualityScore 26.390884/79, exposes run/project/experiment/sample/audit links, lists the recorded run, and that the persisted Run reached through the run link carries the same values (56 metrics). The screenshot is kept outside the repository because it shows the restricted conformance image; its identity is in the observation JSON.  
**State:** ACCEPTED (observation `atg/evidence/2026-08-17-console-observation-01.json`, exit 0).

### QS-A10 — Reproducible Phase-0 evaluation (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.test_reproduction_real
~~~

**Acceptance:** a real Run of the registered engine on the registered r-01 image is recorded, then reproduced by a clean re-execution after re-verifying every recorded engine, model, configuration, and input digest; the reproduction attestation is `REPRODUCED/EXACT` over 56 compared metrics with timing excluded; the immutable Run manifest export (Run, project, experiment, sample, audit events) is byte-consistent with the store; the same flows run through `python -m qualsight.cli reproduce|export-run`.  
**State:** ACCEPTED (3/3 real tests). Parent QS-A03 is ACCEPTED on the strength of these authorized, provenance-complete Runs.

### QS-B10 — Project lifecycle and persistence (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_project_lifecycle_real
~~~

**Acceptance:** `qualsight/projects.py` implements New→Open→Save/Save-As→Close→Clone→Archive as append-only project versions with one audit event per transition; the scientific configuration captured at creation (registry identity, engine identity, configuration digest, model hashes, scope) is byte-identical through every version, clone, and save-as; a real OFIQ Run recorded inside the project stays referenced from every later version and from clones' `derived_from.run_references`; archived projects refuse new Runs (409) until reopened; the console's Projects panel drives the same transitions (observation 03: v1 NEW → run → v2 SAVED → v3 SAVED (close) → v4 OPEN → v5 ARCHIVED, run reference retained).  
**State:** ACCEPTED (2/2 real API tests + observation 03).

### QS-B11 — Real dataset import and manifest validation (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_dataset_import_real
~~~

**Acceptance:** `qualsight/datasets.py` imports the registered LFW funneled dataset from its registered image manifest — 13,233 JPEGs verified in place by SHA-256 and size, 5,749 identities mapped from the directory layout, factual rights `NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET` with the owner receipt and UMass protocol documents as rights evidence, PRD §11 statistics (samples/subject median 1, max 530, 1,680 subjects with ≥2 samples; 3,000/3,000 pairs over 7,701 unique protocol samples; 0 missing/unreadable/mismatched files; all 250×250) with detector-dependent items reported `NOT_ASSESSED`; re-import is idempotent; archived projects refuse import. The deterministic real subset fixture `resources/manifests/lfw-subset-fixture-v1.json` (View 2 fold 1: first five mated + first five non-mated pairs → 16 real images) regenerates byte-identically from the registered manifests.  
**State:** ACCEPTED (5/5 real API tests).

### QS-B12 — Batch quality execution (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_quality_batch_real
~~~

**Acceptance:** `qualsight/engines/ofiq/batch.py` writes verified list files in a job directory, launches the registered engine per chunk through the CLI adapter's batch form (`-c <configuration> -i <generated .txt> -o <new csv>`), tracks progress from the engine's CSV, cancels with process-group termination, records children CPU/RSS/wall use, and persists one row per selected sample to Parquet (28 native/scalar/failed triples) plus a `COMPLETED` batch Run only when every engine process exited 0 without cancellation. Real tests: the 16-image subset fixture yields a result or explicit failure per image; a real cancellation of a 530-image subject batch leaves explicit `NOT_ASSESSED` rows and no surviving engine process; the real image `Abdoulaye_Wade_0003.jpg` yields OFIQ's `nan`/`-1` sentinel on UnderExposurePrevention and OverExposurePrevention, kept as explicit measure failures with `batch.measure_level_failures = 2`.  
**State:** ACCEPTED (4/4 real API tests). Child QS-B12.LFW.FULL below records the full 13,233-image batch.

### QS-B12.LFW.FULL — Full registered LFW batch (executing)

**Safe gate**

~~~sh
env QUALSIGHT_DATA_DIR=/mnt/projects/ofiqgui/var QUALSIGHT_REPOSITORY_ROOT=/mnt/projects/ofiqgui PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli batch --project-id <project> --dataset-id <dataset> --selection all --chunks 1
~~~

**Acceptance:** one registered engine process assessed all 13,233 imported LFW samples in job `job:sha256:5c6b51c8…cccbf` (2026-08-17 14:31→15:58 -05:00): 13,233 `MEASURED`, 0 engine row failures, 0 integrity failures, 0 not assessed; 25 measure-level engine failures (UnderExposurePrevention 12, OverExposurePrevention 12, BackgroundUniformity 1) kept explicit inside MEASURED rows; UnifiedQualityScore scalar mean 51.73 (min 2, max 97); resource use wall 5,187.0 s, children user CPU 130,929.6 s, max RSS 1,144,168 KiB; Parquet `402d88fb…7696`; COMPLETED Run `run:sha256:0521ae4f…0833`.  
**State:** ACCEPTED as evidence child.

### QS-B13 — Quality explorer and drilldown (executing, visual)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_quality_batch_real.RealQualityBatchTests.test_explorer_distribution_reconciles_to_batch_rows_and_drills_down_to_real_samples
python3 scripts/observe_console.py --screenshot-dir <outside the repository> --observation atg/evidence/2026-08-17-console-observation-04.json
~~~

**Acceptance:** `/api/jobs/{job}/distribution` histograms one measure over the job's Parquet with DuckDB and reports outcome totals so bin counts reconcile to batch rows (the test recomputes every bin from the Parquet); `/api/jobs/{job}/drilldown` resolves a bin to its real samples; `/api/samples/{id}/quality` resolves a sample to its measures, Run, audit trail, and locally served image (recorded dataset samples are served only after re-hashing against their recorded identity); dataset statistics gain a live quality-coverage section. The console's Dataset · Batch · Explorer panel imports LFW, runs the subset batch with live progress, draws the UnifiedQualityScore histogram, drills into the top bin (thumbnails of the real samples), and shows a selected sample's 28 measures with Run/audit links (observation 04).  
**State:** ACCEPTED (real test + observation 04).

### QS-B14 — Phase-1 accepted artifact (executing, visual)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -t .
python3 scripts/observe_console.py --screenshot-dir <outside the repository> --observation atg/evidence/2026-08-17-console-observation-04.json
~~~

**Acceptance:** one integrated real-data workflow in the real console — create project → single r-01 assessment recorded into it → save/close/open/archive/reopen → import the registered LFW dataset with full byte verification → run the subset batch to completion → explore the distribution → drill to a real sample → resolve its Run and audit trail — passes end to end (observation 04, exit 0), with the whole backend suite green.  
**State:** ACCEPTED.

### QS-C20 — Verification comparison and protocol schemas (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_protocol_real
~~~

**Acceptance:** `qualsight/domain/comparison.py` keeps protocol label, score, processing failure, threshold policy, provenance marker, and 1:N candidate entities apart with schema guards (a MATED pair cannot join different subjects; a comparison carries either a finite score or an explicit failure; target-rate threshold policies validate their domain; a probe cannot be its own gallery candidate); `qualsight/protocols.py` freezes the registered LFW View 2 list over the imported dataset — 6,000 pairs (3,000/3,000), ten folds of 600 (300 mated), 7,701 unique samples, 4,281 subjects, every side resolved to a recorded sample with the manifest's digest, three official UMass authority documents bound, category `UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA`; refreezing is idempotent; a non-LFW dataset is refused.  
**State:** ACCEPTED (3/3 real API tests).

### QS-C22 — Three matchers × two preprocessing conditions (executing, GPU)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.test_matcher_runtime_real tests.test_matcher_matrix_real tests.api.test_matcher_api_real
~~~

**Acceptance:** the frozen runtime (`qualsight/matchers/`) loads the three registered checkpoints with `torch.load(weights_only=True)` and `load_state_dict(strict=True)` into the exact upstream architectures acquired at setup time from pinned commits (`resources/manifests/matcher-upstream-sources-v1.json`; AdaFace `net.py` d8114b3c, insightface `iresnet.py` 7fadd420 for ElasticFace-Arc R100, TransFace `vit.py`/`backbones/__init__.py` fb6cd56d for TransFace-S with `img_size=112, patch_size=9, embed_dim=512, depth=12, num_heads=8, drop_path_rate=0.05, norm_layer="ln", mask_ratio=0.0`), the registered SCRFD-10G ONNX (byte-verified, CPUExecutionProvider, threshold 0.5, NMS 0.4, port of insightface scrfd.py), and insightface's five-point ArcFace `norm_crop` (Umeyama similarity, template digest bound); the two frozen conditions are implemented as specified in this document; no random weights, non-strict loads, key mapping, implicit downloads, or network calls exist at runtime. The six leaves over the ten fixture pairs and over the full frozen protocol keep every pair with a score or explicit failure and record matcher and preprocessing identities separately (child QS-C22.MATRIX.LFW below).  
**State:** ACCEPTED (5 + 3 + 3 real tests, observation 05).

### QS-C22.MATRIX.LFW — Six immutable Runs over the full frozen LFW View 2 protocol (executing, GPU)

**Safe gate**

~~~sh
env QUALSIGHT_DATA_DIR=/mnt/projects/ofiqgui/var QUALSIGHT_REPOSITORY_ROOT=/mnt/projects/ofiqgui PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli matrix --project-id <project> --protocol-id <protocol>
~~~

**Acceptance:** matrix `matrix:sha256:3769cdaf…e3be` (2026-08-17 15:59→16:13 -05:00) over 6,000 pairs / 7,701 samples produced six COMPLETED Runs. Shared-crop leaves (adaptive canvas 256 for LFW, largest face, JPEG-95): QS-C22.AS `run:sha256:53eb37fe…`, QS-C22.ES `b28df1cb…`, QS-C22.TS `ecc11123…`, each 6,000/6,000 scored; native leaves (fixed 640 canvas, highest confidence, in-memory): QS-C22.AN `a84b2e31…`, QS-C22.EN `ae05a9cc…`, QS-C22.TN `44335ee0…`, each 5,957 scored with 43 explicit `FAILURE_TO_DETECT` pairs. EER (failures excluded / failures as reject): AS 0.02100/0.02100, AN 0.02383/0.03000, ES 0.02100/0.02100, EN 0.02349/0.02967, TS 0.02167/0.02167, TN 0.02349/0.03000; FNMR at FMR ≤ 0.001 (failures excluded, Clopper–Pearson 95 %): AS 0.02367 [0.01853, 0.02976], AN 0.02550 [0.02015, 0.03182], ES 0.02333 [0.01823, 0.02939], EN 0.02483 [0.01955, 0.03108], TS 0.02333 [0.01823, 0.02939], TN 0.02517 [0.01985, 0.03145]; View 2 ten-fold accuracy (threshold on nine folds): AS 0.98783 ± 0.00539, AN 0.98708 ± 0.00393, ES 0.98783 ± 0.00503, EN 0.98725 ± 0.00425, TS 0.98750 ± 0.00511, TN 0.98674 ± 0.00435. Each Run binds the checkpoint and detector digests, the architecture-source digests, the condition definition digest, the ArcFace template digest, the comparisons Parquet, the embeddings file, and the metrics JSON. These are QualSight-executed results on the funneled LFW release with SCRFD alignment; they are not comparable to the matchers' published LFW figures, which use different alignment and image releases.  
**State:** ACCEPTED as evidence child.

### QS-C23 — Verification metric engine (executing)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.test_matcher_matrix_real.RealMatcherMatrixTests.test_metrics_reconcile_to_an_independent_recomputation tests.api.test_matcher_api_real
~~~

**Acceptance:** `qualsight/metrics/verification.py` implements the registry definitions `biometric.verification.{fmr,fnmr,roc,det,eer}@1.0.0` with explicit score direction, the `ACCEPT_WHEN_SCORE_GE_THRESHOLD` equality rule, two recorded failure policies (`EXCLUDE_FAILED_TRIALS` with FTA reported; `FAILURES_AS_REJECT`), FMR/FNMR at every recorded threshold (ROC/DET points), EER by interpolation between adjacent recorded thresholds, operating points at target FMR 0.1/0.01/0.001, exact Clopper–Pearson 95 % intervals with the stated independence caveat, score-distribution summaries, and the View 2 ten-fold accuracy; the metric evidence digest reproduces from an independent recomputation over the Parquet comparisons and every count/limitation is recorded per Run.  
**State:** ACCEPTED.

### QS-C24 — Verification UI and failure drilldown (executing, visual)

**Safe gate**

~~~sh
python3 scripts/observe_console.py --screenshot-dir <outside the repository> --observation atg/evidence/2026-08-17-console-observation-05.json
~~~

**Acceptance:** the console's Verification panel freezes the protocol, runs the matrix, shows all three matchers under both preprocessing conditions side by side (never pooled) with EER, FNMR at FMR ≤ 0.1 %, ten-fold accuracy, scored/failed counts, a DET overlay of the six leaves, and — for a selected leaf and threshold — every false match, false non-match, and processing failure as real pairs with both images, subjects, score, label, threshold rule, condition, transform, and Run link (observation 05: 6 leaves, 6 DET polylines, 5 real false non-matches of QS-C22.AS at threshold 0.9 resolved to their samples).  
**State:** ACCEPTED.

### QS-D31.R0 — Pair-quality policies and ERC over the six real Runs (evidence child; parents QS-D30/QS-D31 stay dependency-blocked)

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_matcher_api_real.RealMatcherApiTests.test_erc_reconciles_to_an_independent_recomputation_over_real_quality_and_scores
~~~

**Acceptance:** `qualsight/metrics/utility.py` implements the six PRD §21 pair-quality policies (min, max, mean, geometric mean, enrolment, probe; version 1.0.0) and the registry ERC `biometric.quality.utility.erc@1.0.0` with a fixed matcher threshold taken from a recorded operating point (or caller value), worst-quality-first rejection with ties leaving together, explicit excluded units, and separate FNMR-ERC (mated units) and FMR-ERC (non-mated units); AU-ERC and partial AU-ERC are reported only as derived summaries labelled as unregistered definitions. `GET /api/matcher-runs/{run}/erc` computes it against any recorded quality job; the API test recomputes the curve independently. Real evidence over the six full-protocol Runs and the full batch job (UnifiedQualityScore scalar, threshold at FMR ≤ 0.001, policy min): FNMR at 0 / 10 % / 20 % rejection — AS 0.02367 / 0.02199 / 0.01917, AN 0.02550 / 0.02358 / 0.02178, ES 0.02333 / 0.02162 / 0.01917, EN 0.02483 / 0.02284 / 0.02136, TS 0.02333 / 0.02162 / 0.01917, TN 0.02517 / 0.02284 / 0.02136 (files `var/matrix/matrix_sha256_3769cdaf…/erc/QS-C22_*-erc.json`; digests in the ledger).  
**State:** ACCEPTED as evidence child; superseded for reuse by the QS-D30–QS-D34 whole-capability sections below (cycle C20), which extend it to registered areas, uncertainty, error models, and policy analysis.

### QS-D30 — Pair-quality policy registry

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_matcher_api_real.RealMatcherApiTests.test_utility_analysis_run_is_recorded_traceable_and_independently_verified
curl -s http://127.0.0.1:8480/api/pair-quality-policies
~~~

**Acceptance:** `qualsight.metrics.utility.PairQualityPolicy` (version 1.0.0) is the versioned registry of the six PRD §21 policies — min, max, mean, geometric mean, enrolment (left sample), probe (right sample) — each with a recorded definition string; `GET /api/pair-quality-policies` serves it; every utility analysis records the policy id and version in its Run configuration and writes the traceable per-pair values (`quality_left`, `quality_right`, `pair_quality`) to `pair_quality.parquet`, served by `GET /api/utility-analyses/{id}/pairs` with links to both samples' quality Runs. Sensitivity to the aggregation rule is evaluable by running the same analysis under another policy (the analysis id embeds the policy).
**State:** ACCEPTED (cycle 2026-08-17-C20; real evidence: six analyses over the six full LFW Runs × the full OFIQ batch under `pair.quality.min`, and the fixture-pair API test).

### QS-D31 — ERC, AU-ERC, partial AU-ERC, and risk-coverage engine

**Safe gate**

~~~sh
/home/aaron/.local/bin/uv run --frozen --offline python scripts/validate_metric_registry.py metrics/metric-registry.json
env PYTHONPATH=src:.. QUALSIGHT_INPUT_RESOURCE_IDS="ofiq-r-01-conformance-input,lfw-funneled-local-candidate" /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli utility --project-id project:sha256:5564b31f9da7c3b89895a449c07a049a9bafa222d10543ff143ecc0f369b8d52 --matcher-run-id <one of the six leaf Runs> --quality-job-id job:sha256:5c6b51c8230390c7fdb2384474c2b0cc4612041fadf0ad46980fb58843dcccbf
env PYTHONPATH=src /home/aaron/.local/bin/uv run --directory backend --frozen --offline python ../scripts/verify_utility_analysis.py <the six analysis directories under var/analyses>
~~~

**Acceptance:** metric registry v1.1.0 (14 definitions; validator and test extended) registers `biometric.quality.utility.au-erc@1.0.0`, `partial-au-erc@1.0.0`, `risk-coverage@1.0.0`, `quality-conditioned-fnmr@1.0.0`, `quality-conditioned-fmr@1.0.0`, `quality-error-association@1.0.0`, `failure-predictor@1.0.0`, and `biometric.policy.threshold-analysis@1.0.0`, each with a frozen semantic contract. `qualsight.analysis.utility.UtilityAnalysisJob` writes an immutable analysis Run (kind ANALYSIS) whose `utility.json` records the rejection domain `[0, 1]` and partial interval `[0, 0.2]`, the risk-coverage re-parameterization (coverage = 1 − rejected fraction), the fixed matcher threshold with provenance (recorded operating point at FMR ≤ 10⁻³ or caller value; never re-estimated), the pair-quality policy, unit counts and excluded units, and cluster-bootstrap (left-subject clusters, 1,000 replicates, seed 20260817) percentile intervals for AU-ERC and partial AU-ERC on FNMR and FMR units separately. Real evidence (six analyses, `pair.quality.min`, UnifiedQualityScore, `analysis` version 1.0.1): AU-ERC(FNMR) AS 0.014744, AN 0.016427, ES 0.014591, EN 0.016154, TS 0.014582, TN 0.016292; partial AU-ERC 0–20 % AS 0.004386, AN 0.004639, ES 0.004346, EN 0.004525, TS 0.004338, TN 0.004535 — the native condition carries higher area than the shared crop for every matcher. `scripts/verify_utility_analysis.py` re-derived every ERC point, both areas, every quality bin, the ROC-AUC (pair counting), and the Spearman coefficient (ranks) of all six analyses from their pair tables with plain-Python loops: 213 checks each, zero mismatches.
**State:** ACCEPTED (cycle 2026-08-17-C20). Analysis Runs: AS `run:sha256:6005f48a…`, AN `run:sha256:17d506e1…`, ES `run:sha256:03050293…`, EN `run:sha256:1da63210…`, TS `run:sha256:96232098…`, TN `run:sha256:1c95ca0d…` (analysis ids `086d0409…`, `a799734e…`, `5fcd5c17…`, `6ea2951b…`, `79fe4b43…`, `15c41732…`; artifact hashes in the ledger).

### QS-D32 — Uncertainty and quality-error models

**Safe gate**

~~~sh
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest -v tests.api.test_matcher_api_real.RealMatcherApiTests.test_utility_analysis_run_is_recorded_traceable_and_independently_verified
~~~

**Acceptance:** `qualsight/metrics/statistics.py` (numpy only) supplies the executable statistical components: Clopper–Pearson exact intervals, cluster bootstrap by subject (unit, replicates, seed, undefined-replicate count recorded; UNSUPPORTED_BY_SAMPLE when fewer than half the replicates are defined), Spearman with Fisher-z interval, Kendall τ-b with tie counts, Newton–Raphson logistic regression of the error indicator on pair quality with Wald intervals and an odds ratio per 10 quality units that runs only when the recorded support rule (≥ 10 events and ≥ 10 non-events per parameter) is met, ROC-AUC (Mann–Whitney ranks), PR-AUC (average precision), and sensitivity/specificity/precision/recall at the recorded quality-policy threshold; quality-conditioned FNMR/FMR bins with fewer than 30 units are flagged as unsupported precision. On the real full-protocol analyses the models are supported (AS: 71 false non-matches among 3,000 mated units; Spearman ρ(quality, score) 0.559 [0.534, 0.584]; τ-b 0.397; odds ratio per 10 UQS 0.775 [0.681, 0.883]; ROC-AUC of negative pair quality for false non-match 0.640, PR-AUC 0.037 at prevalence 2.37 %; across the six analyses ROC-AUC spans 0.619–0.640, ρ 0.532–0.611, τ-b 0.376–0.440, odds ratio per 10 UQS 0.775–0.801 with 70–76 events); on the ten-pair fixture the same code reports UNSUPPORTED_BY_SAMPLE for the logistic model and unsupported precision for every bin, which the API test asserts.
**State:** ACCEPTED (cycle 2026-08-17-C20).

### QS-D33 — Threshold-policy analysis

**Safe gate**

~~~sh
env PYTHONPATH=src /home/aaron/.local/bin/uv run --directory backend --frozen --offline python ../scripts/verify_utility_analysis.py <analysis directory>
~~~

**Acceptance:** `qualsight/metrics/policy.py` evaluates the PRD §24 operating policy `pair quality ≥ Q` at a fixed MATCHER THRESHOLD `T` and reports retention rate, rejection rate, FMR and FNMR before and after the policy with exact binomial intervals, number rejected, errors prevented (false non-matches and false matches separately), successful comparisons removed (mated and non-mated separately), and the per-quality-measure impact table over all 28 OFIQ scalar measures at retention-matched cutoffs (ties handled by the smallest admissible distinct cutoff; the matcher threshold is unchanged). Threshold kinds are labelled MATCHER_THRESHOLD and QUALITY_POLICY_THRESHOLD in the document and the UI and are never re-estimated during policy analysis. Real evidence at Q = 22 (the min-UQS cutoff at 20 % mated rejection) and T at FMR ≤ 10⁻³: AS retains 4,678 of 6,000 units (78.0 %), FNMR 2.37 % → 1.92 %, 26 errors prevented (25 FNM, 1 FM), 1,296 successful comparisons removed; AN 2.55 % → 2.18 %, 24 prevented, 1,284 removed; ES 2.33 % → 1.92 %, 25/1,297; EN 2.48 % → 2.14 %, 24/1,284; TS 2.33 % → 1.92 %, 25/1,297; TN 2.52 % → 2.14 %, 25/1,283. `scripts/verify_utility_analysis.py` re-derived every count by brute force from the pair tables (exact match, six analyses).
**State:** ACCEPTED (cycle 2026-08-17-C20).

### QS-D34 — Phase-3 gate (quality-to-utility accepted artifact)

**Safe gate**

~~~sh
python3 scripts/observe_console.py --screenshot-dir <outside the repository> --observation atg/evidence/2026-08-17-console-observation-06.json
env PYTHONPATH=src:.. /home/aaron/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -q
python3 -m unittest discover -s tests -q
~~~

**Acceptance:** the console's Quality → utility panel lists the recorded analyses of the project, runs a new analysis from the picked matcher Run, quality job, measure, pair policy, target FMR, quality threshold, and bootstrap count, overlays the FNMR-ERC of every recorded analysis, and shows for the selected analysis the two thresholds by kind, the four areas with cluster-bootstrap intervals, the policy table, the quality-conditioned bins with support flags, the association and prediction statistics, the per-measure impact table, and the rejected / prevented / removed pairs with images and links to both samples' quality Runs and to the analysis Run. Observation 06 (fixture pairs, real headless Chromium) recorded one analysis row, one ERC polyline, the policy table (30 % retained at Q = 40, 7 rejected, 0 errors prevented, 7 successful comparisons removed), 10 conditioned bins, 28 per-measure rows, and six rejected pairs resolving to real LFW pairs; exit 0. Every number shown traces to `utility.json` / `policy.json` / `pair_quality.parquet` of an immutable analysis Run that binds the source matcher Run's matcher, preprocessing, and model hashes and the quality job's engine binding.
**State:** ACCEPTED (cycle 2026-08-17-C20). Backend gate 77/77, root gate 37/37 (recorded in the ledger).

Historical accepted events QS-A02.R0, QS-A02.R1, QS-A02.R2, QS-A02.R3, QS-A02.R4, QS-A02.R5, QS-A02.R6, QS-A03.R0, QS-A04.R0, QS-A06.R0, and QS-A06.R1M retain their exact recorded counts and hashes in `atg/ledger.jsonl`; current refinements supersede them for reuse without rewriting history. The active accepted evidence set is `{QS-A00, QS-A01, QS-A02, QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03, QS-A03.R1, QS-A04, QS-A04.R1, QS-A04.R2, QS-A05, QS-A06, QS-A06.R1O, QS-A06.R2, QS-A07, QS-A08, QS-A09, QS-A10, QS-B10, QS-B11, QS-B12, QS-B12.LFW.FULL, QS-B13, QS-B14, QS-C20, QS-C22, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, QS-C23, QS-C24, QS-C25, QS-D30, QS-D31, QS-D31.R0, QS-D32, QS-D33, QS-D34}`. Twenty whole capabilities participate in coarse dependency readiness; none is dependency-ready while QS-C21 lacks its real input.

## Dependency-ready parallel groups

At the capability level:

- at present: QS-E40 is dependency-ready; QS-E41–QS-E43, QS-F50–QS-F54, and QS-H70–QS-H73 follow in dependency order; QS-C21 stays parked on a registered real external score artifact (owner input) and blocks only QS-H74;
- in Phase 5: QS-F51, QS-F52, and QS-F53 may proceed in parallel after their predecessors;
- in Phase 7: QS-H70, QS-H71, QS-H72, and QS-H73 may proceed in parallel when file ownership is disjoint.

Any loop body that mutates shared files is sequential. Parallel analysis does not permit overlapping patches.

## Explicit blocked inputs

The following are real blockers, not empty planning fields:

1. **Real external matcher scores (owner input):** bounded local discovery found no face-relevant score artifact; `/mnt/projects/02_perception_biometrics/ofiq_29794/data/scores` contains only `.gitkeep`. QS-C21 needs one registered real externally computed score artifact over a real protocol. Owner decision 28 (2026-08-17) closed Phase 2 on QualSight-executed Runs and parked QS-C21 as a direct predecessor of QS-H74, so it now blocks only the beta terminal gate; QS-C20, QS-C22, QS-C23, and QS-C24 are accepted.
2. **Matcher training-set identity exclusion:** recorded as each matcher's published claim by owner decision 19; QualSight does not verify it and every LFW report must carry the caveat.
3. **Native-condition detection failures:** the fixed 640-canvas condition misses 43 protocol pairs' faces on the 250×250 funneled images while the adaptive canvas finds all; the failures are explicit in the six Runs and are a real finding about the condition, not a defect to hide.
4. **Non-beta authorities:** controlled-data handling requirements and the IJB-C agreement signer remain unresolved. They block those optional data scopes but do not block the open-data beta path unless activated.

Resource, rights, and effective-scope evidence is recorded by QS-A02 before downstream execution. No blocker above may be bypassed with a substitute dataset, unpinned download, random weights, or an unregistered derivative.

## Beta terminal gate

QS-H74 may run only when all beta predecessors are ACCEPTED. Its compiled subgraph must execute this real chain:

~~~text
create project
→ import approved identity-labeled real dataset
→ run OFIQ
→ inspect quality and components
→ import one registered real external score artifact with permanent external-computation provenance (QS-C21, parked by decision 28 until a real artifact is registered)
→ run AdaFace, ElasticFace, and TransFace under both frozen preprocessing conditions
→ preserve all official mated and non-mated pairs plus explicit processing failures in six immutable Runs
→ compute FMR, FNMR, ROC, DET, EER, operating points, and uncertainty separately for all six Runs
→ compute ERC with an explicit pair-quality and threshold policy
→ compare matcher utility and failure overlap only within a fixed preprocessing condition
→ compare preprocessing effects only within a fixed matcher and protocol
→ inspect real disagreement and failure samples permitted for display
→ save provenance-bearing dashboard
→ generate report and figure/data exports
→ export and validate reproduction bundle
→ trace every number and plot to its immutable Run
~~~

Acceptance requires:

- no manual Python analysis outside the registered execution adapters;
- all commands exit 0;
- full test and validation gates match or improve their recorded baselines;
- every scientific acceptance field in PRD section 58 is present;
- every number and plot satisfies PRD section 60;
- restricted images are excluded from outputs unless explicitly authorized;
- the UI and reports show both preprocessing conditions and never pool them into one result;
- the six matcher-condition Runs use the same ordered real sample IDs, subject IDs, and pair manifest, preserving null scores for explicit failures;
- the final runtime observation uses the real pipeline without a substitute path.

## Pre-execution thought experiment

| Check | Result |
|---|---|
| Consistency | Pass: the beta path implements PRD sections 55 and 57; advanced Phase 6 cannot block beta |
| Missing step | Pass with explicit blocks: unavailable real datasets and matcher resources stop their dependent nodes |
| Tool appropriateness | Pass for the current frontier: static gates validate real repository/resource bytes, locks, schemas, and archive identities; executing gates launch only the registered installed OFIQ v1.1.2 runtime on registered inputs under the owner receipt; no matcher or detector executes yet |
| Dependency validation | Pass: every edge consumes a predecessor’s schema, resource, execution, metric, or artifact output |
| Termination | Pass: all iteration occurs inside the bounded loops in ATG_LOOP.md |
| Loop interface | Pass: each cycle consumes a frozen ready batch and produces accepted evidence or one explicit non-success state |
| Constraint compliance | Pass: only registered real data and complete production paths are permitted; no commercial SDK dependency, cloud runtime, or unauthorized outward action is required |

Known limitation: the accepted evidence children prove only their stated predicates; the console is a single-page workbench rather than the full PRD §44 navigation, the matcher matrix runs in one process without chunked progress persistence, and no imported-score, utility, cross-matcher, reporting, or packaging path has executed. ATG_LOOP.md prevents blocked capabilities from entering a cycle until refinement supplies literal actions, registered real inputs, and complete Definition-of-Ready evidence.

## Level history

- **L-1:** frozen decision record in this document and ATG_LOOP.md;
- **L0:** one product graph from canonical requirements to accepted beta;
- **L1:** Phase 0 → 1 → 2 → 3 → 4 → 5 → 7 → beta, with Phase 6 post-beta;
- **L2:** 41-node, 85-edge acyclic capability graph QS-A00 through QS-H74 (v1.6: QS-C21 rewired to feed QS-H74 directly by decision 28); post-beta nodes remain outside the beta DAG;
- **L3 current frontier:** Phases 0–3 accepted (Phase 2 under decision 28; QS-A00 through QS-A10, QS-B10 through QS-B14, QS-C20, QS-C22, QS-C23, QS-C24, QS-C25, QS-D30 through QS-D34) with current accepted evidence children QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, and QS-D31.R0; QS-C21 and QS-C25 blocked; superseded resource/domain/execution slices remain in the append-only ledger;
- **future L3 refinements:** created one ready capability at a time by the bounded controller, preserving its L2 input/output interface.

Only the minimal historical subgraph is replaced when a leaf fails. Accepted nodes and their evidence remain frozen.
