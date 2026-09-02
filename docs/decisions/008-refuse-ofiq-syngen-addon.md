# ADR 008: Do not admit the `ofiq-syngen` package into the beta add-on catalog

Date: 2026-09-01

## Context

The requested “OFIQSyn” capability was investigated as an add-on for controlled degradation of real
recorded images. The only exact public package candidate found was PyPI `ofiq-syngen` 0.5.2. PyPI
identifies it as an alpha “synthetic face image quality degradation pipeline” and points its homepage,
repository, documentation, and issue tracker to `https://github.com/astoreyai/ofiq-syngen`.

The 0.5.2 artifacts observed on 2026-09-01 were:

| Artifact | Size | SHA-256 |
|---|---:|---|
| `ofiq_syngen-0.5.2-py3-none-any.whl` | 149,478 bytes | `075a89c35113940cd348d6170af4065c2918a2b7dba48a40a537c107f4a9165f` |
| `ofiq_syngen-0.5.2.tar.gz` | 7,617,815 bytes | `de5bd6fbf34a60c23d8d548cf8887530343127d993fecc2c1ac47f3d832d5fbe` |

The wheel declares MIT for its own code and lists restrictive third-party model and dataset notices.
Its advertised GitHub repository returned HTTP 404 during review. Its packaged metadata contains an
unresolved pending DOI marker. The wheel includes synthetic second-face insertion, synthetic headwear,
diffusion pipelines that load remote models, randomized occlusion/noise operators, asset-download code,
and license-gated FLAME/BFM/DECA paths. Importing the central component registry also imports the
generative operators.

The candidate is not the BSI/eu-LISA OFIQ reference implementation. Official OFIQ measures real input
quality; QualSight already runs the reviewed OFIQ 1.1.2 binary and records all 28 measures.

## Decision

Do not install, import, vendor, download at runtime, or list `ofiq-syngen` 0.5.2 as a QualSight add-on.
The package conflicts with the governing prohibition on synthetic or randomly generated data,
unresolved placeholders, arbitrary package loaders, unreviewed network acquisition, and ungoverned
restricted assets. A closed driver that exposed only part of the wheel would still depend on an
unavailable source authority and a distribution whose central registry imports prohibited paths.

QualSight continues to expose its seven code-owned deterministic transformations over recorded real
images: JPEG compression, Gaussian blur, downscale, gamma, contrast, rotation, and occlusion. These
operations have bounded parameter contracts, recorded parent/output hashes, and no generative identity
path. Their UI label must not imply they are official OFIQ algorithms; OFIQ measures the resulting
images independently.

## Reconsideration criteria

Reconsider only a specific future artifact that has all of the following evidence:

- an available authoritative source repository and immutable source identity;
- no synthetic identity, generative image, random-data, placeholder, or unbounded loader path;
- exact package and asset hashes, sizes, licenses, and acquisition receipts;
- a closed code-owned driver with bounded offline execution over recorded real inputs;
- real-image deterministic, tamper, resource-limit, and provenance tests; and
- a capability that is not a misleading duplicate of an existing registered transform.

Until then, the package is a policy refusal rather than an unfinished beta implementation.

## Sources reviewed

- PyPI package metadata: <https://pypi.org/project/ofiq-syngen/0.5.2/>
- Advertised source repository, unavailable during review: <https://github.com/astoreyai/ofiq-syngen>
- Official OFIQ project: <https://github.com/BSI-OFIQ/OFIQ-Project>
