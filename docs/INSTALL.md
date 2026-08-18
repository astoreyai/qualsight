# QualSight installation (controlled workstation)

QualSight installs from pinned resources and then runs with the network disabled. Installation is
the only phase that touches the network; every fetched byte is checked against a recorded SHA-256
before it is used.

## Reference workstation

* Linux x86_64 (verified: Debian 12, kernel 6.1), NVIDIA GPU optional (the matcher runtime uses CUDA
  when available and CPU otherwise; OFIQ runs on CPU).
* Python 3.11 with [`uv`](https://docs.astral.sh/uv/) 0.9 (`~/.local/bin/uv`); Node 20 with pnpm 10
  (`/usr/bin/node` — a newer Node in `~/.local/bin` must not shadow it; the scripts prepend `/usr/bin`).
* GCC 12 and CMake for the one-time OFIQ v1.1.2 build (Conan 2.18 is installed as a `uv` tool by
  `scripts/ofiq_v112_build.sh`; no `sudo`).
* The registered inputs at their registry paths (`resources/resource-registry.json`), acquired by the
  owner under the recorded use decisions and never bundled with QualSight: the LFW funneled release
  and its official pair lists, the AdaFace IR-101 / ElasticFace-Arc R100 / TransFace-S checkpoints,
  the SCRFD-10G ONNX detector, and the OFIQ v1.1.2 source tag with its model archive. Every
  artifact's expected hash is in the registry; `scripts/validate_resource_registry.py` verifies them.

## One command

```sh
git clone <this repository> qualsight && cd qualsight
bash scripts/install.sh
```

`install.sh` performs, in order: toolchain check → `uv sync --frozen` (backend wheels by lock hash)
→ `pnpm install --frozen-lockfile` and the console build → OFIQ v1.1.2 build if the registered
runtime does not already validate → `qualsight setup` (pinned upstream matcher sources into the data
directory) → registry, metric-registry, and governance validation. It stops at the first failure.

## Start and verify offline operation

```sh
~/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli serve
# console + API on http://127.0.0.1:8480 (loopback only)
python3 scripts/offline_gate.py --observation atg/evidence/offline-gate.json
```

The offline gate starts the product with `QUALSIGHT_NETWORK_GUARD=1` (every non-loopback socket is
refused and counted), then runs the whole workflow through the API — project, LFW import,
single-image assessment, quality batch, protocol freeze, matcher matrix, utility analysis, figure,
report, bundle, reproduction, purge scope, audit — and passes only when every step succeeds and the
guard counted zero attempts. Its last real run is recorded in `atg/evidence/2026-08-17-offline-gate-01.json`.

## Data directory and environment

| Variable | Meaning | Default |
|---|---|---|
| `QUALSIGHT_DATA_DIR` | SQLite store, jobs, matrix, analyses, figures, reports, bundles, exports, upstream sources | `var/` in the repository |
| `QUALSIGHT_ACTOR` | attributed single user for every audited action | `local-user:<login>` |
| `QUALSIGHT_NETWORK_GUARD` | `1` refuses non-loopback sockets at runtime | off |
| `QUALSIGHT_INPUT_RESOURCE_IDS` | registered input resources the engine may read | conformance image + LFW |
| `QUALSIGHT_REGISTRY_PATH` | resource registry | `resources/resource-registry.json` |

## Tests

```sh
python3 -m unittest discover -s tests -q                                              # governance, registries, protocol authority
env PYTHONPATH=src:.. ~/.local/bin/uv run --directory backend --frozen --offline python -m unittest discover -s tests -q   # backend (real engines; ~5 min)
cd frontend && PATH=/usr/bin:$PATH pnpm exec tsc --noEmit -p tsconfig.json && pnpm run build
```

The backend tests use fresh temporary data directories; pinned upstream matcher sources are copied
from the installed `var/upstream` cache (byte-verified) so the suite runs offline after `qualsight setup`.
