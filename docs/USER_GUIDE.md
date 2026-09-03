# QualSight user guide — the canonical workflow

QualSight is a local, offline research workbench that (1) measures face image quality with the registered
OFIQ v1.1.2 engine (ISO/IEC 29794-5 reference), (2) runs six frozen face matchers under two frozen
preprocessing conditions over a frozen 1:1 protocol, (3) relates quality to matcher utility (ERC,
AU-ERC, risk-coverage, error models, threshold-policy analysis), (4) contrasts matchers and
preprocessing, and (5) turns every result into provenance-bearing figures, dashboards, reports,
exports, and reproducibility bundles. Every number is an immutable Run in the local SQLite store and
traces to its inputs by content hash. QualSight performs no automatic egress. Operator-downloaded
reports, exports, and reproduction bundles may include deployment-local provenance; keep them on the
controlled workstation until they pass disclosure review for the intended recipient.

Two front doors exist and produce the same records: the **console** (the installed environment's
`python -m qualsight.cli serve` command in [INSTALL.md](INSTALL.md) → <http://127.0.0.1:8480>) and the
**CLI** (`python -m qualsight.cli …`). This guide gives the CLI
form for every step because the CLI is scriptable and is what `scripts/docs_workflow_gate.py`
executes verbatim: every fenced block tagged `sh gate` below is run, in order, by that gate on the
registered subset fixture (16 LFW images / 10 protocol pairs) with a fresh data directory. The
console equivalents are named in each step.

Prerequisites: [INSTALL.md](INSTALL.md) completed (`scripts/install.sh`), so the OFIQ v1.1.2 runtime,
the LFW funneled release, the AdaFace / ElasticFace / MagFace / TopoFR / TransFace-B / TransFace-S
checkpoints, and the SCRFD detector are
registered at their pinned paths and `qualsight setup` has acquired the pinned upstream matcher
sources. Methods, thresholds, and limitations are defined in
[SCIENTIFIC_METHODS.md](SCIENTIFIC_METHODS.md); the local data controls in [THREAT_MODEL.md](THREAT_MODEL.md).

## Console workflow

The console is task-first. A persistent left rail keeps the active project and recorded evidence
context visible while the operator moves between these stages:

1. **Home** starts an admitted task and shows recent recorded work. A task that lacks a project,
   working sample, protocol, quality job, or matcher Run names that prerequisite instead of opening an
   unusable control surface.
2. **Projects** creates or selects the purpose- and scope-bound workspace. **Inputs** imports an
   already admitted registered collection or records owner-supplied input. Download and verification
   of a missing pair-protocol add-on occurs under **Manage system → Add-ons** before project import.
3. **Inspect quality** uploads or selects one real image. The successful upload becomes the active
   WorkingSample immediately; its preview is served from the hash-verifying sample endpoint. The same
   screen renders SCRFD box/five-point geometry, OFIQ face/98-point geometry, UnifiedQualityScore, the
   other 27 OFIQ measures, native units, and explicit measure failures. Assessing quality opens charts
   and readable evidence; raw JSON remains a secondary disclosure or download.
4. **Compare** records Image A and Image B independently, then records exactly one operator assertion:
   same person, different people, or relationship not recorded. Select a READY matcher and compatible
   preprocessing profile to create an immutable direct-pair comparison. The score, fixed threshold,
   matcher decision, operator assertion, agreement, crops, failures, and provenance remain separate.
   An operator assertion is not publisher ground truth, and an unrecorded relationship supports no
   FMR/FNMR or outcome-correctness claim.
5. **Stress test** starts from the active WorkingSample. It applies any of the seven backend-published,
   bounded deterministic transformations to that exact recorded image, shows the hash-verified original
   and derivatives, and runs OFIQ over the derived set. The resulting trajectories describe one sample;
   the separately headed dataset study is required for population matcher-error claims.
6. **Analyze utility** selects a completed quality job, one measure actually present in its recorded Parquet,
   and a completed matcher Run. Compatible completed analyses can be overlaid only when their protocol
   pair universe, quality evidence, policy, measure, and metric-registry identities agree. A changed
   matcher or preprocessing profile is shown as the comparison axis; incomplete legacy identity cannot
   be guessed.
7. **Results** presents recorded cross-matcher and preprocessing contrasts. **Evidence & reports**
   opens structured in-application records for Runs, jobs, analyses, reports, samples, and lifecycle
   evidence. Raw API JSON is an explicit secondary action. Governed downloads remain local until
   disclosure review approves external transfer.
8. **Manage system** operates the closed add-on catalog. Its Add-ons, Processing profiles, Connections,
   Runtime & storage, and Audit areas expose exact source/checkpoint/archive identities, rights and
   receipt evidence, compatibility, pull/download, load/unload, leases, storage, recovery, and durable
   lifecycle events. Connections has zero providers in this beta because no provider-specific biometric
   egress authority exists.

| Left-navigation label | Route | Primary operator surface |
|---|---|---|
| Home | `#/home` | prerequisites and recent recorded work |
| Projects | `#/projects` | project lifecycle and verified purge |
| Inputs | `#/data` | dataset import, batch quality, and quality explorer |
| Inspect quality | `#/assess` | recorded single-image upload, geometry, measures, and charts |
| Compare | `#/matchers` | direct image pair and dataset matcher study |
| Stress test | `#/advanced` | accepted single-image stress plus visibly separated research extensions |
| Analyze utility | `#/utility` | quality-to-matcher utility and compatible overlays |
| Results | `#/cross` | cross-matcher and preprocessing contrasts |
| Evidence & reports | `#/reports` | typed evidence, figures, reports, governed exports, and bundles |
| Manage system | `#/system` | add-ons, processing profiles, connections, runtime/storage, and audit |

Use the project purge action in **Projects** when owner inputs and their descendants must be removed.
The confirmation shows the backend-published scope and requires the exact project identifier. The
browser clears its active sample, pair, overlays, analyses, and evidence only after the backend returns
`verified=true`; late responses from the purged context are ignored.

## 0. Environment for the CLI

```sh gate
cd "$QUALSIGHT_ROOT"
export PYTHONPATH="$QUALSIGHT_ROOT/backend/src:$QUALSIGHT_ROOT"
export QUALSIGHT_INPUT_RESOURCE_IDS="ofiq-r-01-conformance-input,lfw-funneled-local-candidate"
QS="$UV run --directory backend --frozen --offline python -m qualsight.cli"
$QS setup
```

`QUALSIGHT_DATA_DIR` selects the data directory (default `var/` in the repository); `QUALSIGHT_ACTOR`
sets the attributed user id (default `local-user:<login>`). The network guard is on by default and refuses
non-loopback sockets except during an operator-requested pull of hash-pinned architecture sources or
an operator-requested, owner-admitted, hash-pinned checkpoint or verification-dataset archive. Set
`QUALSIGHT_NETWORK_GUARD=0` only for an explicitly approved unguarded process (see THREAT_MODEL.md).

The console's **Manage system → Add-ons** and **Manage system → Connections** views are the beta's closed
allowlisted catalog. They report six
matcher drivers, one identity-labeled LFW dataset driver, three pair-verification dataset drivers,
three matcher-preprocessing profiles (two SCRFD source-image profiles plus the exact publisher-
prealigned ArcFace-112 pass-through), seven controlled-degradation operators, and no admitted remote API
connections. It verifies all six registered
checkpoints and their pinned architecture sources, and exposes explicit source pull, checkpoint
acquisition, real model load, and unload actions. For each matcher, QualSight can import verified
owner-held bytes or stream the exact author-published file into private managed storage, verifies its
registered size and SHA-256 before no-clobber installation, and records a durable receipt. The owner
authorization is limited to local noncommercial research and does not grant redistribution. The
author-published MagFace verification archive is likewise hash-pinned, safely extracted, and exposed as
separate LFW, CFP-FP, and AgeDB-30 pair protocols without inventing identity labels. Direct acquisition
of the identity-labeled LFW tree remains unavailable because that record lacks an admitted live
source/redirect and receipt contract. After a matcher matrix, the same
view inventories and re-hashes each project embedding
artifact and links to its Run and governed exports.

This is not a package marketplace. Drivers are code-owned and allowlisted so an add-on cannot execute
arbitrary Python, native commands, or network calls in the API process. Remote API connections are not
admitted in this local-only beta; admission would require an approved data-flow and classification,
operator-held secrets outside provenance, endpoint pinning, explicit egress policy, and a local adapter
with the same immutable Run contract.

## 1. Create a project (console: *Projects → New project*)

A project fixes the purpose scope every downstream Run must be approved for. The registered LFW
release and the six matchers approve `QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1`; the OFIQ
conformance image approves `REPRODUCE_OFIQ_CONFORMANCE_TESTS`.

```sh gate
PROJECT_ID=$($QS project-create --name "User guide workflow" --description "Canonical workflow executed from docs/USER_GUIDE.md" --scope QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["project_id"])')
echo "$PROJECT_ID"
```

## 2. Import the registered dataset (console: **Inputs → Dataset → Import real dataset**)

Import records every sample (path, SHA-256, size, mtime, subject from the directory name) and the
dataset statistics; `--skip-hash-verification` trusts the registry's already-verified hashes instead of
re-hashing 13,233 files.

```sh gate
DATASET_ID=$($QS import-lfw --project-id "$PROJECT_ID" --skip-hash-verification | python3 -c 'import json,sys; print(json.load(sys.stdin)["dataset_id"])')
echo "$DATASET_ID"
```

## 3. Assess one image and run the quality batch (console: **Inspect quality**, **Inputs → Batch quality**)

`assess` measures one registered image with the registered engine and records a single-sample Run;
`batch` measures dataset samples in list-file mode and writes `quality_measurements.parquet`
(28 measures × native / scalar / failed) plus one COMPLETED Run when every engine process exits 0.
The subset fixture is the registered 16-image slice; use `--selection all` for the full release.

```sh gate
$QS assess --image "$OFIQ_R01_IMAGE" --scope REPRODUCE_OFIQ_CONFORMANCE_TESTS | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["run_id"], {k: v for k, v in d["metrics"].items() if "UnifiedQualityScore" in k})'
JOB_ID=$($QS batch --project-id "$PROJECT_ID" --dataset-id "$DATASET_ID" --selection subset_fixture --chunks 1 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["job_id"])')
echo "$JOB_ID"
```

## 3b. Bring your own inputs (decision 30; console: **Inspect quality → Upload image**, **Inputs → Import real dataset**)

An operator-picked photo or directory is admitted by SHA-256 (uploads are stored content-addressed
in the data directory; directory files are recorded in place), recorded with source kind
`OWNER_SUPPLIED_LOCAL_INPUT` and the attributed operator, and assessed by the same registered
engine. Owner-supplied data is never redistributed, never presented as a registered rights-reviewed
dataset, and is removed by the project purge. `freeze-owner-pairs` builds a deterministic seeded
pair protocol from the operator's identity labels (the generated pair list is itself hashed and
recorded); the matcher matrix then runs on it unchanged. Admission preflights the real identity counts
and refuses more than 20,000 source samples or 20,000 output pairs before materializing pair records.
The gate block below uses real registered
LFW photographs copied outside the registry tree to stand in for an evaluator's own files.

```sh gate
OWNER_ROOT=$(mktemp -d)
mkdir -p "$OWNER_ROOT/person_a" "$OWNER_ROOT/person_b"
cp /mnt/projects/datasets/lfw/lfw_funneled/Gerhard_Schroeder/Gerhard_Schroeder_0001.jpg "$OWNER_ROOT/person_a/"
cp /mnt/projects/datasets/lfw/lfw_funneled/Gerhard_Schroeder/Gerhard_Schroeder_0002.jpg "$OWNER_ROOT/person_a/"
cp /mnt/projects/datasets/lfw/lfw_funneled/Jacques_Chirac/Jacques_Chirac_0001.jpg "$OWNER_ROOT/person_b/"
cp /mnt/projects/datasets/lfw/lfw_funneled/Jacques_Chirac/Jacques_Chirac_0002.jpg "$OWNER_ROOT/person_b/"
$QS assess-owner --project-id "$PROJECT_ID" --image "$OWNER_ROOT/person_a/Gerhard_Schroeder_0001.jpg" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["source_kind"] == "OWNER_SUPPLIED_LOCAL_INPUT"; print(d["run_id"], d["image_sha256"][:16])'
OWNER_DATASET_ID=$($QS import-owner-directory --project-id "$PROJECT_ID" --root "$OWNER_ROOT" --name "evaluator directory" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dataset_id"])')
$QS freeze-owner-pairs --project-id "$PROJECT_ID" --dataset-id "$OWNER_DATASET_ID" --seed 7 | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["mated"] >= 2 and d["nonmated"] >= 2; print(d["protocol_id"], d["pairs_sha256"][:16])'
rm -rf "$OWNER_ROOT"
```

## 4. Freeze the protocol and run the matcher matrix (console: **Compare → Dataset study**)

The LFW View 2 protocol (6,000 pairs, 10 folds) is frozen from the registered official pair lists
into an immutable protocol record. The matrix runs AdaFace IR-101, ElasticFace-Arc R100, MagFace
IResNet-100, TopoFR R100, TransFace-B, and TransFace-S under both preprocessing conditions
(`SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1` and
`SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1`) and records twelve leaf Runs, each keeping
every protocol pair (a pair without two templates carries an explicit processing failure).
`--fixture-pairs-only` restricts to the registered 10-pair fixture; omit it for the full protocol.

In the console, the matcher and preprocessing checkboxes select the complete Cartesian product for
that Run. For example, one matcher and either preprocessing profile records one leaf; six matchers
and both profiles records twelve. The selection is stored with the resulting immutable Runs and never
changes a global preprocessing setting. Advanced 1:N and degradation analyses use the same catalog
for their single matcher/profile selection. Controlled degradation operators are separate: they
create an explicitly derived dataset and do not silently replace matcher preprocessing.

The CLI exposes the same per-Run product through repeatable `--matcher` and `--preprocessing` flags.
The canonical source-image gate names both compatible SCRFD profiles explicitly so the separate
publisher-prealigned pass-through cannot be selected for source images.

```sh gate
PROTOCOL_ID=$($QS freeze-lfw-protocol --project-id "$PROJECT_ID" --dataset-id "$DATASET_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["protocol_id"])')
$QS matrix --project-id "$PROJECT_ID" --protocol-id "$PROTOCOL_ID" --fixture-pairs-only --preprocessing SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1 --preprocessing SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1 > "$WORK/matrix.json"
MATRIX_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["matrix_id"])' "$WORK/matrix.json")
AS_RUN=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(l["run_id"] for l in d["leaves"] if l["leaf"]=="QS-C22.AS"))' "$WORK/matrix.json")
echo "$MATRIX_ID $AS_RUN"
```

Read the twelve-leaf metrics (EER, FNMR at FMR ≤ 10⁻¹/10⁻²/10⁻³ with Clopper–Pearson intervals, ROC/DET,
View-2 ten-fold accuracy) in the console's matrix table, or export them (step 7).

## 4b. Import externally computed scores (console: **Compare → Dataset study → Externally computed scores**)

QualSight can also evaluate scores that some other system produced over the same frozen protocol
(a vendor SDK, a collaborator's pipeline, or an independent open-source pipeline run outside
QualSight). The artifact must be registered (`kind: EXTERNAL_SCORE_ARTIFACT`; one CSV with at least
`pair_id,score`, optionally `left_path,right_path,label,failure`, plus a provenance sidecar). The
importer maps rows to protocol pairs by id, refuses unknown/duplicate rows or paths and labels that
disagree with the protocol, keeps uncovered pairs as `NOT_EXECUTED`, and records an
`EXTERNALLY_COMPUTED` Run evaluated with the same registered definitions — never re-scored.

```sh gate
$QS import-external-scores --project-id "$PROJECT_ID" --protocol-id "$PROTOCOL_ID" --resource-id external-scores-lfw-view2-insightface-buffalo-l > "$WORK/external.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["leaf"], d["provenance_marker"], d["scored"], d["failed"], d["eer"])' "$WORK/external.json"
```

## 5. Quality → utility (console: **Analyze utility**)

A utility analysis joins one matcher Run with one quality job under an explicit pair-quality policy
and records ERC, AU-ERC and partial AU-ERC with subject-cluster bootstrap intervals, risk-coverage,
quality-conditioned FNMR/FMR bins, association statistics, quality-as-failure-predictor summaries,
and the threshold-policy analysis (pair quality ≥ Q at the fixed MATCHER THRESHOLD T). The default T
is the recorded operating point at FMR ≤ 10⁻³; the default Q is the min-pair-quality cutoff at 20 %
mated rejection. Verify any analysis independently with `scripts/verify_utility_analysis.py`.

```sh gate
$QS utility --project-id "$PROJECT_ID" --matcher-run-id "$AS_RUN" --quality-job-id "$JOB_ID" --matcher-threshold 0.8 --quality-threshold 40 --replicates 100 > "$WORK/utility.json"
UTILITY_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["analysis_id"])' "$WORK/utility.json")
UTILITY_RUN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$WORK/utility.json")
UTILITY_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["directory"])' "$WORK/utility.json")
$UV run --directory backend --frozen --offline python ../scripts/verify_utility_analysis.py "$UTILITY_DIR"
```

## 6. Cross-matcher contrasts (console: **Results**)

Matcher contrasts hold the crop fixed; preprocessing contrasts hold the matcher fixed. The analysis
records error overlap (A only / B only / both / neither), failure-set Jaccard, paired-bootstrap AU-ERC
differences, a quality × matcher interaction, threshold transferability, score agreement, and a
54,000-row contrast pair table for the workbench. Verify with `scripts/verify_cross_matcher.py`.

```sh gate
$QS cross-matcher --project-id "$PROJECT_ID" --matrix-id "$MATRIX_ID" --quality-job-id "$JOB_ID" --replicates 100 > "$WORK/cross.json"
CROSS_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["directory"])' "$WORK/cross.json")
SNAPSHOT_PATH="$QUALSIGHT_DATA_DIR/cross-verifier-$(date -u +%Y%m%dT%H%M%SZ)-$$.snapshot.sqlite3"
$QS snapshot-database --output "$SNAPSHOT_PATH"
$UV run --directory backend --frozen --offline python ../scripts/verify_cross_matcher.py "$CROSS_DIR" "$SNAPSHOT_PATH"
rm -- "$SNAPSHOT_PATH"
```

## 7. Figures, exports, bundles, reproduction (console: **Evidence & reports**)

Figures render PNG/SVG/PDF/CSV plus `figure.provenance.json`; the console builds dashboards and
HTML/PDF reports from the same records. A console export button submits an origin-checked, same-origin
`POST` to `/api/projects/{project_id}/exports/{category}?ref=&format=csv|json|parquet`. The API first
returns a verified creation receipt to the console; the browser then saves that exact recorded file from
the receipt's read-only project-scoped link. This preserves a visible API error if creation is refused and
does not hold the governed table in a client-created blob URL. Exports carry Run identifiers on every row.
A `.qsexp` bundle packages a Run for reproduction, and
`reproduce-bundle` re-executes it and reports EXACT / WITHIN_TOLERANCE / DIFFERENT /
MISSING_DEPENDENCY / MISSING_DATA / BLOCKED (exit 0 only for the first two).

Every downloaded report, export, or `.qsexp` bundle is governed evidence, not a disclosure-approved
deliverable. It can contain deployment-local provenance paths even though browser JSON is path-free.
Review the downloaded bytes and intended recipient before any external transfer.

```sh gate
$QS figure --project-id "$PROJECT_ID" --kind det_overlay --params "{\"matrix_id\": \"$MATRIX_ID\"}"
BUNDLE=$($QS bundle-export --run-id "$UTILITY_RUN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')
$QS reproduce-bundle "$BUNDLE" > "$WORK/reproduction.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["state"], d["compared_values"]); sys.exit(0 if d["state"] in ("EXACT","WITHIN_TOLERANCE") else 1)' "$WORK/reproduction.json"
```

## 7b. Research extensions outside beta acceptance (console: **Stress test**): 1:N identification, dataset degradation, container execution

*1:N identification (console: **Stress test → Research extensions → Freeze 1:N protocol → Search**).* The LFW identification
protocol enrols every subject with ≥ 2 images (first image) and probes the rest; single-image
subjects are open-set non-mated probes. The search embeds gallery and probes with the frozen matcher
on the GPU, computes the full probe × gallery similarity there, keeps rank-depth candidate lists, and
records closed-set rank-k/CMC/true-mate rank, open-set FPIR/FNIR/TPIR at target FPIR (thresholds from
the non-mated searches; unsupported precision flagged), gallery-size effects, and the probe-quality
effect when a quality job is given (`biometric.identification.*@1.0.0`, registry v1.2.0).

```sh gate
$QS identification --project-id "$PROJECT_ID" --dataset-id "$DATASET_ID" --matcher-id adaface-ir101-local-candidate --selection subset_fixture --rank-depth 20 --quality-job-id "$JOB_ID" > "$WORK/identification.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["search_id"], d["summary"]["gallery_size"], d["summary"]["mated_searches"], d["summary"]["nonmated_searches"], d["rank_k"])' "$WORK/identification.json"
```

*Dataset degradation (console: **Stress test → Research extensions → Create derived set → OFIQ over derived → Analyse response**).*
`POST /api/projects/{id}/datasets/{did}/transformations` derives JPEG / Gaussian-blur / downscale /
gamma / contrast / rotation / occlusion levels from registered real samples with a complete chain
(root resource, parent sample and hash, operator kind/version/parameters, encoding); the derived
dataset is measured by the registered OFIQ engine (the batch engine admits derived samples only when
the store records their chain), and `POST /api/projects/{id}/degradation-analyses` records the
quality, matcher-score, FNMR/FMR responses per level with slope, area, threshold crossing, and failure
onset. Derived images inherit the source restrictions and never leave the workstation.

*Container execution (QS-X64).* `batch --execution container` (API: `"execution": "container"`) runs the
same registered OFIQ runtime inside a `FROM scratch` image that QualSight builds from the registered
binary tree plus the workstation's own C/C++ runtime libraries (hashed and recorded), with
`--network none`, a read-only root filesystem, no capabilities, and the caller's uid; the Run records
the image id and rootfs manifest hash. Results equal the host adapter's byte for byte.

## 8. Reading the records

* `GET /api/projects/{id}/runs/{run_id}` — the immutable Run (bindings, model hashes, environment, metrics, artifacts).
* `GET /api/projects/{id}/audit` — every meaningful action with the attributed user and timestamp.
* `GET /api/projects/{id}/purge-scope` and `POST …/purge` — the application-level verified purge
  described in THREAT_MODEL.md.
* Verification metrics follow `metrics/metric-registry.json` (v1.1.0, 14 definitions); thresholds
  are always labelled MATCHER THRESHOLD or QUALITY POLICY THRESHOLD and are never conflated.

## 9. What the numbers are — and are not

LFW results here are QualSight-executed on the funneled release with SCRFD alignment under two frozen
crops; they are not reproductions of the matchers' published LFW figures. Matcher training-set
identity exclusion is each matcher's published claim (owner decision 19), not something QualSight
verifies. Comparison units that share subjects are not independent; subject-cluster bootstrap
intervals are reported where computed and Wald intervals are approximations. Bins with fewer than
30 units are flagged as unsupported precision rather than reported as precise.
