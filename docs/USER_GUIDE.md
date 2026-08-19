# QualSight user guide — the canonical workflow

QualSight is a local, offline research workbench that (1) measures face image quality with the registered
OFIQ v1.1.2 engine (ISO/IEC 29794-5 reference), (2) runs three frozen face matchers under two frozen
preprocessing conditions over a frozen 1:1 protocol, (3) relates quality to matcher utility (ERC,
AU-ERC, risk-coverage, error models, threshold-policy analysis), (4) contrasts matchers and
preprocessing, and (5) turns every result into provenance-bearing figures, dashboards, reports,
exports, and reproducibility bundles. Every number is an immutable Run in the local SQLite store and
traces to its inputs by content hash. Nothing leaves the workstation.

Two front doors exist and produce the same records: the **console** (`qualsight serve` →
<http://127.0.0.1:8480>) and the **CLI** (`python -m qualsight.cli …`). This guide gives the CLI
form for every step because the CLI is scriptable and is what `scripts/docs_workflow_gate.py`
executes verbatim: every fenced block tagged `sh gate` below is run, in order, by that gate on the
registered subset fixture (16 LFW images / 10 protocol pairs) with a fresh data directory. The
console equivalents are named in each step.

Prerequisites: [INSTALL.md](INSTALL.md) completed (`scripts/install.sh`), so the OFIQ v1.1.2 runtime,
the LFW funneled release, the AdaFace / ElasticFace / TransFace checkpoints, and the SCRFD detector are
registered at their pinned paths and `qualsight setup` has acquired the pinned upstream matcher
sources. Methods, thresholds, and limitations are defined in
[SCIENTIFIC_METHODS.md](SCIENTIFIC_METHODS.md); the local data controls in [THREAT_MODEL.md](THREAT_MODEL.md).

## 0. Environment for the CLI

```sh gate
cd "$QUALSIGHT_ROOT"
export PYTHONPATH=src:..
export QUALSIGHT_INPUT_RESOURCE_IDS="ofiq-r-01-conformance-input,lfw-funneled-local-candidate"
QS="$UV run --directory backend --frozen --offline python -m qualsight.cli"
$QS setup
```

`QUALSIGHT_DATA_DIR` selects the data directory (default `var/` in the repository); `QUALSIGHT_ACTOR`
sets the attributed user id (default `local-user:<login>`); `QUALSIGHT_NETWORK_GUARD=1` refuses any
non-loopback socket at runtime (see THREAT_MODEL.md).

## 1. Create a project (console: *Projects → New project*)

A project fixes the purpose scope every downstream Run must be approved for. The registered LFW
release and the three matchers approve `QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1`; the OFIQ
conformance image approves `REPRODUCE_OFIQ_CONFORMANCE_TESTS`.

```sh gate
PROJECT_ID=$($QS project-create --name "User guide workflow" --description "Canonical workflow executed from docs/USER_GUIDE.md" --scope QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["project_id"])')
echo "$PROJECT_ID"
```

## 2. Import the registered dataset (console: *Dataset → Import registered LFW*)

Import records every sample (path, SHA-256, size, mtime, subject from the directory name) and the
dataset statistics; `--skip-hash-verification` trusts the registry's already-verified hashes instead of
re-hashing 13,233 files.

```sh gate
DATASET_ID=$($QS import-lfw --project-id "$PROJECT_ID" --skip-hash-verification | python3 -c 'import json,sys; print(json.load(sys.stdin)["dataset_id"])')
echo "$DATASET_ID"
```

## 3. Assess one image and run the quality batch (console: *Sample inspector*, *Quality batch*)

`assess` measures one registered image with the registered engine and records a single-sample Run;
`batch` measures dataset samples in list-file mode and writes `quality_measurements.parquet`
(28 measures × native / scalar / failed) plus one COMPLETED Run when every engine process exits 0.
The subset fixture is the registered 16-image slice; use `--selection all` for the full release.

```sh gate
$QS assess --image "$OFIQ_R01_IMAGE" --scope REPRODUCE_OFIQ_CONFORMANCE_TESTS | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["run_id"], {k: v for k, v in d["metrics"].items() if "UnifiedQualityScore" in k})'
JOB_ID=$($QS batch --project-id "$PROJECT_ID" --dataset-id "$DATASET_ID" --selection subset_fixture --chunks 1 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["job_id"])')
echo "$JOB_ID"
```

## 3b. Bring your own inputs (decision 30; console: *Assess your own photo*, *Data → owner import*)

An operator-picked photo or directory is admitted by SHA-256 (uploads are stored content-addressed
in the data directory; directory files are recorded in place), recorded with source kind
`OWNER_SUPPLIED_LOCAL_INPUT` and the attributed operator, and assessed by the same registered
engine. Owner-supplied data is never redistributed, never presented as a registered rights-reviewed
dataset, and is removed by the project purge. `freeze-owner-pairs` builds a deterministic seeded
pair protocol from the operator's identity labels (the generated pair list is itself hashed and
recorded); the matcher matrix then runs on it unchanged. The gate block below uses real registered
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

## 4. Freeze the protocol and run the matcher matrix (console: *Verification*)

The LFW View 2 protocol (6,000 pairs, 10 folds) is frozen from the registered official pair lists
into an immutable protocol record. The matrix runs AdaFace IR-101, ElasticFace-Arc R100, and
TransFace-S under both preprocessing conditions (`SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1`
and `SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1`) and records six leaf Runs, each keeping
every protocol pair (a pair without two templates carries an explicit processing failure).
`--fixture-pairs-only` restricts to the registered 10-pair fixture; omit it for the full protocol.

```sh gate
PROTOCOL_ID=$($QS freeze-lfw-protocol --project-id "$PROJECT_ID" --dataset-id "$DATASET_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["protocol_id"])')
$QS matrix --project-id "$PROJECT_ID" --protocol-id "$PROTOCOL_ID" --fixture-pairs-only > "$WORK/matrix.json"
MATRIX_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["matrix_id"])' "$WORK/matrix.json")
AS_RUN=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(next(l["run_id"] for l in d["leaves"] if l["leaf"]=="QS-C22.AS"))' "$WORK/matrix.json")
echo "$MATRIX_ID $AS_RUN"
```

Read the six-leaf metrics (EER, FNMR at FMR ≤ 10⁻¹/10⁻²/10⁻³ with Clopper–Pearson intervals, ROC/DET,
View-2 ten-fold accuracy) in the console's matrix table, or export them (step 7).

## 4b. Import externally computed scores (console: *Verification → Externally computed scores*)

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

## 5. Quality → utility (console: *Quality → utility*)

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

## 6. Cross-matcher contrasts (console: *Cross-matcher*)

Matcher contrasts hold the crop fixed; preprocessing contrasts hold the matcher fixed. The analysis
records error overlap (A only / B only / both / neither), failure-set Jaccard, paired-bootstrap AU-ERC
differences, a quality × matcher interaction, threshold transferability, score agreement, and a
54,000-row contrast pair table for the workbench. Verify with `scripts/verify_cross_matcher.py`.

```sh gate
$QS cross-matcher --project-id "$PROJECT_ID" --matrix-id "$MATRIX_ID" --quality-job-id "$JOB_ID" --replicates 100 > "$WORK/cross.json"
CROSS_DIR=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["directory"])' "$WORK/cross.json")
$UV run --directory backend --frozen --offline python ../scripts/verify_cross_matcher.py "$CROSS_DIR" "$QUALSIGHT_DATA_DIR/qualsight.sqlite3"
```

## 7. Figures, exports, bundles, reproduction (console: *Figures · dashboards · reports*)

Figures render PNG/SVG/PDF/CSV plus `figure.provenance.json`; the console builds dashboards and
HTML/PDF reports from the same records; exports (`/api/exports/{category}?ref=&format=csv|json|parquet`)
carry Run identifiers on every row; a `.qsexp` bundle packages a Run for reproduction, and
`reproduce-bundle` re-executes it and reports EXACT / WITHIN_TOLERANCE / DIFFERENT /
MISSING_DEPENDENCY / MISSING_DATA / BLOCKED (exit 0 only for the first two).

```sh gate
$QS figure --project-id "$PROJECT_ID" --kind det_overlay --params "{\"matrix_id\": \"$MATRIX_ID\"}"
BUNDLE=$($QS bundle-export --run-id "$UTILITY_RUN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')
$QS reproduce-bundle "$BUNDLE" > "$WORK/reproduction.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["state"], d["compared_values"]); sys.exit(0 if d["state"] in ("EXACT","WITHIN_TOLERANCE") else 1)' "$WORK/reproduction.json"
```

## 7b. Advanced analysis (post-beta): 1:N identification, controlled degradation, container execution

*1:N identification (console: Advanced → Freeze 1:N protocol → Search).* The LFW identification
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

*Controlled degradation (console: Advanced → Create derived set → OFIQ over derived → Analyse response).*
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

* `GET /api/runs/{run_id}` — the immutable Run (bindings, model hashes, environment, metrics, artifacts).
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
