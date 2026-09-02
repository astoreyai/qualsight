# QualSight installation (controlled workstation)

QualSight installs on the controlled reference workstation from pinned resources and then runs with
the network disabled. This is not a relocatable cold-start package. The current registry contains
453 absolute-path values (199 unique) that bind controlled scientific inputs, provenance evidence,
and native runtimes to this deployment. `scripts/release.sh` therefore labels the archive as a
controlled single-user workstation beta and refuses to imply a turnkey portable install.

Network access is admitted only during explicit installation or add-on acquisition. Every fetched
runtime, source, checkpoint, or dataset archive is checked against its recorded byte length and
SHA-256 before use. Scientific execution does not download dependencies implicitly.

## Reference workstation

* Linux x86_64 (verified: Debian 12, kernel 6.1), NVIDIA GPU optional (the matcher runtime uses CUDA
  when available and CPU otherwise; OFIQ runs on CPU).
* Python 3.11.2 with [`uv`](https://docs.astral.sh/uv/) 0.9.26 on `PATH`; the project-pinned
  `pip-audit` 2.10.0 and Playwright 1.58.0 are installed into the release environment by the frozen
  development group; Node 26.7.0 with pnpm 10.28.0. Release and installation use the reviewed `node`
  selected on `PATH` and refuse any other version before dependency mutation.
  The nine-workflow release validator also requires the compatible Playwright-managed Chromium executable;
  Python dependency installation alone does not install that browser, and the gate fails closed if it is absent.
* The already registered OFIQ v1.1.2 native runtime, validated by its recorded artifact hashes. The
  repository now carries the reviewed Conan 2.18.1 lock, GCC 12 release profile, exact 15-node
  dependency closure, and upstream archive identities used by `scripts/ofiq_v112_build.sh`. The
  builder can resolve from a verified local cache without network access or acquire only the exact
  governed archives and locked packages when network access is explicitly allowed. A rebuild at a
  different deployment path still requires review and registry repinning before execution.
* The registered inputs at their registry paths (`resources/resource-registry.json`), acquired by the
  owner under the recorded use decisions and never bundled with QualSight: the LFW funneled release
  and its official pair lists, the AdaFace IR-101 / ElasticFace-Arc R100 / MagFace IResNet-100 /
  TopoFR R100 / TransFace-B / TransFace-S checkpoints,
  the SCRFD-10G ONNX detector, and the OFIQ v1.1.2 source tag with its model archive. Every
  artifact's expected hash is in the registry; `scripts/validate_resource_registry.py` verifies them.

## Reference-workstation command

```sh
bash scripts/install.sh
env PYTHONPATH="$PWD/backend/src:$PWD" uv run --directory backend --frozen python -m qualsight.cli setup
```

The command requires the repository at the deployment root recorded by the controlled registry and all
deployment-local registry artifacts to be present. A fresh clone elsewhere is expected to stop at
registry validation; that refusal is an integrity control, not a successful portable installation.

`install.sh` performs, in order: toolchain check → `uv sync --frozen` (backend wheels by lock hash)
→ `pnpm install --frozen-lockfile` and the console build → byte-validation of the registered OFIQ
v1.1.2 runtime, with an explicit stop if that runtime is absent or drifted → metric-registry validation.
It stops at the first failure. ATG governance validation runs only in the clean controlled checkout and
committed-tree release gate before source-archive projection. The installer does not download matcher,
detector, or dataset
add-ons and does not require optional add-ons that are irrelevant to installation of the core runtime.

Add-on downloads are explicit operator actions after installation. The console's **Manage system → Add-ons**
view is the normal surface; `qualsight setup` is the equivalent explicit CLI acquisition action for the
pinned upstream matcher sources. Either route retains the resource registry's hash, size, rights, and
no-clobber checks.

The console's **Manage system → Add-ons** view can install any of the six exact registered checkpoints into
`QUALSIGHT_DATA_DIR/checkpoints/`. Each action either imports already verified owner-held bytes or
downloads the author-published bytes from its admitted HTTPS endpoint; both paths verify the registered
size and SHA-256 before no-clobber installation and write a durable receipt. The same view downloads,
hash-verifies, and safely extracts the author-published LFW, CFP-FP, and AgeDB-30 pair archive into the
private data directory. Publisher availability is external: a quota or rate-limit response is refused
as non-checkpoint data and surfaced to the operator. Direct download of the identity-labeled LFW tree
remains unavailable. The SCRFD detector is verified and executed from its registered external path
but is not cold-start downloadable from the current console. QualSight never bundles any checkpoint
or dataset bytes.

**Manage system → Processing profiles** lists the three per-Run preprocessing choices separately from the
seven deterministic image transformations. **Manage system → Connections** contains no provider action in
this beta. **Manage system → Runtime & storage** shows live health, registry, database/schema, storage,
bounded-work, recovery, and the held data-directory process lease. **Manage system → Audit** is the durable
receipt and lifecycle-error authority for source pull, acquisition, load, unload, and refusal events.

These two gaps define the package boundary: the identity-labeled LFW tree and SCRFD/native OFIQ
bootstrap remain controlled-workstation prerequisites, and the absolute-path registry has no
relocation overlay. The source, console, dependency SBOM, checksums, and release manifest can be
packaged once the worktree is clean; a portable end-user installer cannot honestly be emitted from
the current registry contract.

## Release archive

`bash scripts/release.sh` accepts only a clean committed tree, extracts that commit to a private staging
directory, performs frozen all-group Python and integrity-checked pnpm installs there, rebuilds the console
from those committed bytes,
and creates source and console archives plus a deterministic CycloneDX 1.5 inventory. The inventory
contains real uv/pnpm dependency edges and installed-package declared-license evidence, registry-only
scientific and native resource metadata, explicit rights/admission states, and an explicit marker for
blocked native resolution. It does not claim that external resource bytes or a complete host/native OS
package inventory are bundled. The release also records every command and exit code in the complete
15-gate beta chain, including the executable user guide and nine-workflow browser aggregate, plus an
artifact integrity manifest, the commit/tree IDs, and SHA-256 checksums. SBOM,
gate, vulnerability, and manifest inputs are read from that immutable staged tree.
Before publication, the SBOM, vulnerability report, beta-gate record, manifest, commit record, and
checksum file are scanned for secrets and raw or encoded private build paths.
A nonblocking release lock and one atomic no-clobber directory rename publish the complete set beneath
`dist/qualsight-<version>/`; an existing versioned directory is never overwritten. The source archive is
derived from the exact commit but excludes the internal session/ATG records, machine-specific performance
records, and recorded real-result fixtures listed by
`resources/security/release-package-policy-v1.json`, so those internal and derived records do not enter the
package. The archive projects operator-home locators to the external prefix declared in its release policy and retains only the
allowlisted controlled-host bindings and public acquisition pins. Because that projected registry is not
runtime authority, installation from the archive requires `QUALSIGHT_REGISTRY_PATH` to the reviewed
unprojected registry. The source archive is therefore a controlled-host package, not portable or generally
redistributable. The archive contains no
biometric images, embeddings, model checkpoints,
detector, or native OFIQ bytes: archive creation and extracted-package validation both fail closed on
forbidden suffixes, disguised binary signatures, exact registered runtime/model/configuration hashes,
nonregular members, encoded operator-home values, screenshot paths, credential filenames, and
high-confidence secret patterns. The console build disables source maps and is scanned for private build
paths before archiving. `QUALSIGHT-PACKAGE-CONTENTS.json` binds every projected regular file by sorted
relative path, source commit, SHA-256, byte length, and normalized safe mode. Release staging uses a
deterministic safe umask, extracts without inheriting ownership or permissions, and runs the trusted
committed-tree validator against that extracted tree and expected commit before publication. Extraction
may only reduce group/world permissions while preserving required owner access; permission additions,
special bits, or group/world write are refused. Release
dependency mutation begins only after the exact pinned Python, uv, Node, and pnpm versions pass preflight.
The package carries no cryptographic publisher signature.

If a beta gate returns nonzero, the terminal distinguishes passed, failed, and not-run gates and names the
first failed gate using only its exit code plus stdout/stderr byte counts and SHA-256 digests. Before the
disposable release stage is removed, `release.sh` retains that same path-normalized gate record as a mode-0600
file beneath a unique mode-0700 `dist/.release-failures/qualsight-<version>.<commit>.*` directory. Captured
stdout/stderr is limited to the final 4,000 characters, with controlled roots replaced by stable labels and
remaining absolute home, mount, or temporary paths removed. Biometric inputs, embeddings, checkpoints, and
temporary working records are not retained. These private diagnostics are not release artifacts and are never
copied into the versioned package directory. When a committed stage executes the user guide, the gate preserves
the caller's validated `QUALSIGHT_REPOSITORY_ROOT`; it does not reinterpret deployment-bound authorization paths
relative to the disposable stage.

## Start and verify offline operation

After the explicit `qualsight setup` command has verified and cached every pinned architecture source,
and with no QualSight server running, execute the offline gate. The gate requires an observation
destination, claims port 8480, starts its own loopback server, consumes only the verified local cache,
runs the complete real-data workflow, and stops that server before returning:

```sh
python3 scripts/offline_gate.py --observation atg/evidence/offline-gate.json
env PYTHONPATH="$PWD/backend/src:$PWD" uv run --directory backend --frozen --offline python -m qualsight.cli serve
# console + API is now available on http://127.0.0.1:8480 (loopback only)
```

The gate starts the product with its network guard enabled by default (every non-loopback socket is
refused and counted), then runs the whole workflow through the API — project, LFW import,
single-image assessment, quality batch, protocol freeze, matcher matrix, utility analysis, figure,
report, bundle, reproduction, purge scope, audit — and passes only when every step succeeds and the
guard counted zero attempts. The release gate writes a new no-clobber observation for the exact committed
tree instead of accepting an older development-tree observation.

The release browser aggregate separately executes all nine real console workflows with the built
frontend and Playwright-managed Chromium. Use fresh work and evidence paths:

```sh
BROWSER_GATE_ROOT=$(mktemp -d)
BROWSER_WORK="$BROWSER_GATE_ROOT/work"
"$PWD/.venv/bin/python" scripts/validate_release_browser.py \
  --root "$PWD" \
  --work-root "$BROWSER_WORK" \
  --output "$BROWSER_GATE_ROOT/browser-aggregate.json" \
  --python "$PWD/.venv/bin/python"
```

The aggregate requires the reviewed external registry and the verified upstream cache. It fails closed
when the built console, registered real inputs, Node 26.7.0, or Chromium runtime is unavailable.

## Data directory and environment

| Variable | Meaning | Default |
|---|---|---|
| `QUALSIGHT_DATA_DIR` | SQLite store, jobs, matrix, analyses, figures, reports, bundles, exports, upstream sources | `var/` in the repository |
| `QUALSIGHT_ACTOR` | attributed single user for every audited action | `local-user:<login>` |
| `QUALSIGHT_NETWORK_GUARD` | `0` explicitly permits non-loopback sockets; all other values refuse them | on |
| `QUALSIGHT_MATCHER_DEVICE` | force the frozen matcher device (`cuda` / `cpu`); unset = CUDA when ≥ 1.5 GiB is free, else CPU (recorded per Run) | unset |
| `QUALSIGHT_INPUT_RESOURCE_IDS` | registered input resources the engine may read | conformance image + LFW |
| `QUALSIGHT_REGISTRY_PATH` | resource registry | `resources/resource-registry.json` |
| `QUALSIGHT_SOURCE_CACHE_ROOT` | existing absolute, non-symlinked directory holding manifest-verified upstream matcher source bytes; never grants network access | repository `var/upstream` |

## Controlled-checkout tests

The release source archive intentionally excludes `tests/` and `backend/tests/`; these commands apply
only to the clean controlled Git checkout that produced the archive. Validate an extracted package with
the package, install, offline-workflow, and browser-workflow gates described above. The non-mutating
package/toolchain check is:

```sh
bash scripts/install.sh --validate-package-only
```

```sh
.venv/bin/python -m unittest discover -s tests -q                                     # governance, registries, protocol authority
env PYTHONPATH=src:.. uv run --directory backend --frozen --offline python -m unittest discover -s tests -q   # definitive beta-3 candidate: 303 tests, 5 opt-in network skips, 4,047.221 s
cd frontend && pnpm exec tsc --noEmit -p tsconfig.json && pnpm run build
```

The backend tests use fresh temporary data directories; pinned upstream matcher sources are copied
from the selected verified source cache so the suite runs offline after `qualsight setup`.
