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
* Python 3.11 with [`uv`](https://docs.astral.sh/uv/) 0.9 on `PATH`; the project-pinned
  `pip-audit` 2.10.0 is installed into `.venv` by the frozen development group; Node 20 with pnpm 10
  (`/usr/bin/node` — a newer Node in `~/.local/bin` must not shadow it; the scripts prepend `/usr/bin`).
* The already registered OFIQ v1.1.2 native runtime, validated by its recorded artifact hashes. No
  reviewed Conan lockfile exists in this repository or the existing build evidence, so
  `scripts/ofiq_v112_build.sh` stops unconditionally before acquisition, mutation, tool installation,
  or dependency resolution. An operator-supplied path does not establish review authority. A fresh
  native build remains externally blocked until its exact Conan closure is registered and reviewed.
* The registered inputs at their registry paths (`resources/resource-registry.json`), acquired by the
  owner under the recorded use decisions and never bundled with QualSight: the LFW funneled release
  and its official pair lists, the AdaFace IR-101 / ElasticFace-Arc R100 / MagFace IResNet-100 /
  TopoFR R100 / TransFace-B / TransFace-S checkpoints,
  the SCRFD-10G ONNX detector, and the OFIQ v1.1.2 source tag with its model archive. Every
  artifact's expected hash is in the registry; `scripts/validate_resource_registry.py` verifies them.

## Reference-workstation command

```sh
bash scripts/install.sh
```

The command requires the repository at its registered `/mnt/projects/ofiqgui` path and all
deployment-local registry artifacts to be present. A fresh clone elsewhere is expected to stop at
registry validation; that refusal is an integrity control, not a successful portable installation.

`install.sh` performs, in order: toolchain check → `uv sync --frozen` (backend wheels by lock hash)
→ `pnpm install --frozen-lockfile` and the console build → byte-validation of the registered OFIQ
v1.1.2 runtime, with an explicit stop if that runtime is absent or drifted → metric-registry and ATG
governance validation. It stops at the first failure. It does not download matcher, detector, or dataset
add-ons and does not require optional add-ons that are irrelevant to installation of the core runtime.

Add-on downloads are explicit operator actions after installation. The console's **System → Add-ons**
view is the normal surface; `qualsight setup` is the equivalent explicit CLI acquisition action for the
pinned upstream matcher sources. Either route retains the resource registry's hash, size, rights, and
no-clobber checks.

The console's **System → Add-ons** view can install any of the six exact registered checkpoints into
`QUALSIGHT_DATA_DIR/checkpoints/`. Each action either imports already verified owner-held bytes or
downloads the author-published bytes from its admitted HTTPS endpoint; both paths verify the registered
size and SHA-256 before no-clobber installation and write a durable receipt. The same view downloads,
hash-verifies, and safely extracts the author-published LFW, CFP-FP, and AgeDB-30 pair archive into the
private data directory. Publisher availability is external: a quota or rate-limit response is refused
as non-checkpoint data and surfaced to the operator. Direct download of the identity-labeled LFW tree
remains unavailable. The SCRFD detector is verified and executed from its registered external path
but is not cold-start downloadable from the current console. QualSight never bundles any checkpoint
or dataset bytes.

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
13-step beta gate, an artifact integrity manifest, the commit/tree IDs, and SHA-256 checksums. SBOM,
gate, vulnerability, and manifest inputs are read from that immutable staged tree.
A nonblocking release lock and one atomic no-clobber directory rename publish the complete set beneath
`dist/qualsight-<version>/`; an existing versioned directory is never overwritten. The source archive is
derived from the exact commit but excludes the internal session/ATG records, machine-specific performance
records, and recorded real-result fixtures listed by
`resources/security/release-package-policy-v1.json`, so those internal and derived records do not enter the
package. Exact registry paths remain because the controlled deployment resolves its owner-held external
assets through them; the source archive is therefore a controlled-host package, not portable or generally
redistributable. The archive contains no
biometric images, embeddings, model checkpoints,
detector, or native OFIQ bytes: archive creation fails closed on those file classes, nonregular members,
screenshot paths, credential filenames, and high-confidence secret patterns. It carries no cryptographic
publisher signature.

## Start and verify offline operation

```sh
env PYTHONPATH="$PWD/backend/src:$PWD" ~/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli serve
# console + API on http://127.0.0.1:8480 (loopback only)
python3 scripts/offline_gate.py --observation atg/evidence/offline-gate.json
```

The product starts with its network guard enabled by default (every non-loopback socket is
refused and counted), then runs the whole workflow through the API — project, LFW import,
single-image assessment, quality batch, protocol freeze, matcher matrix, utility analysis, figure,
report, bundle, reproduction, purge scope, audit — and passes only when every step succeeds and the
guard counted zero attempts. Its last real run is recorded in `atg/evidence/2026-08-17-offline-gate-01.json`.

## Data directory and environment

| Variable | Meaning | Default |
|---|---|---|
| `QUALSIGHT_DATA_DIR` | SQLite store, jobs, matrix, analyses, figures, reports, bundles, exports, upstream sources | `var/` in the repository |
| `QUALSIGHT_ACTOR` | attributed single user for every audited action | `local-user:<login>` |
| `QUALSIGHT_NETWORK_GUARD` | `0` explicitly permits non-loopback sockets; all other values refuse them | on |
| `QUALSIGHT_MATCHER_DEVICE` | force the frozen matcher device (`cuda` / `cpu`); unset = CUDA when ≥ 1.5 GiB is free, else CPU (recorded per Run) | unset |
| `QUALSIGHT_INPUT_RESOURCE_IDS` | registered input resources the engine may read | conformance image + LFW |
| `QUALSIGHT_REGISTRY_PATH` | resource registry | `resources/resource-registry.json` |

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -q                                     # governance, registries, protocol authority
env PYTHONPATH=src:.. ~/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -q   # backend (real engines; ~5 min)
cd frontend && PATH=/usr/bin:$PATH pnpm exec tsc --noEmit -p tsconfig.json && pnpm run build
```

The backend tests use fresh temporary data directories; pinned upstream matcher sources are copied
from the installed `var/upstream` cache (byte-verified) so the suite runs offline after `qualsight setup`.
