"""Frozen local matcher runtime (QS-C22 preparation).

Each matcher is bound to its registered checkpoint bytes (SHA-256 verified
against the resource registry before loading), deserialized with
``torch.load(weights_only=True)``, and loaded ``strict=True`` into the exact
upstream architecture acquired from the pinned commit. There is no random
initialization, no non-strict fallback, no key mapping, and no network access.
Embeddings are 512-dimensional, finite, L2-normalized; the score is cosine
similarity (higher is more similar).

Frozen transforms (ATG_PLAN):
* ADAFACE_IR101_BGR_NEG1_POS1_L2_V1 — BGR CHW float32 scaled to [-1, 1];
* ELASTICFACE_ARC_R100_RGB_NEG1_POS1_L2_V1 — BGR→RGB CHW float32 in [-1, 1];
* TRANSFACE_S_P9_RGB_NEG1_POS1_L2_V1 — BGR→RGB CHW float32 in [-1, 1].
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path

import numpy as np
import torch

from ..execution.registry_binding import ResourceRegistry
from .upstream import UpstreamSources


class MatcherId(StrEnum):
    ADAFACE_IR101 = "adaface-ir101-local-candidate"
    ELASTICFACE_ARC_R100 = "elasticface-arc-r100-local-candidate"
    TRANSFACE_S = "transface-s-glint360k-local-candidate"


TRANSFORM_IDS: dict[MatcherId, str] = {
    MatcherId.ADAFACE_IR101: "ADAFACE_IR101_BGR_NEG1_POS1_L2_V1",
    MatcherId.ELASTICFACE_ARC_R100: "ELASTICFACE_ARC_R100_RGB_NEG1_POS1_L2_V1",
    MatcherId.TRANSFACE_S: "TRANSFACE_S_P9_RGB_NEG1_POS1_L2_V1",
}
COLOR_ORDER: dict[MatcherId, str] = {
    MatcherId.ADAFACE_IR101: "BGR",
    MatcherId.ELASTICFACE_ARC_R100: "RGB",
    MatcherId.TRANSFACE_S: "RGB",
}
UPSTREAM_KEYS: dict[MatcherId, tuple[str, ...]] = {
    MatcherId.ADAFACE_IR101: ("adaface_net",),
    MatcherId.ELASTICFACE_ARC_R100: ("insightface_arcface_torch_iresnet",),
    MatcherId.TRANSFACE_S: ("transface_vit", "transface_backbones_init"),
}


class MatcherRuntimeError(RuntimeError):
    """Raised when a matcher cannot be frozen faithfully."""


@dataclass(frozen=True, slots=True)
class MatcherBinding:
    matcher_id: MatcherId
    transform_id: str
    checkpoint_path: str
    checkpoint_sha256: str
    checkpoint_size: int
    architecture_source_sha256: tuple[str, ...]
    parameter_count: int
    device: str
    torch_version: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FrozenMatcher:
    def __init__(self, matcher_id: MatcherId, registry: ResourceRegistry, upstream: UpstreamSources, device: str | None = None) -> None:
        self.matcher_id = matcher_id
        resource = registry.resource(matcher_id.value)
        if resource.state not in {"APPROVED_LOCAL_RESEARCH_USE", "VERIFIED_LIMITED_USE"}:
            raise MatcherRuntimeError(f"{matcher_id.value} is {resource.state}; refusing to load")
        models = [item for item in resource.artifacts if item.role == "MODEL"]
        if len(models) != 1 or models[0].identity.path is None:
            raise MatcherRuntimeError(f"{matcher_id.value} must register exactly one MODEL artifact")
        checkpoint = Path(models[0].identity.path)
        digest = sha256_file(checkpoint)
        if digest != models[0].identity.sha256:
            raise MatcherRuntimeError(f"{matcher_id.value}: checkpoint bytes {digest} differ from the registered {models[0].identity.sha256}")
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        source_digests: list[str] = []
        for key in UPSTREAM_KEYS[matcher_id]:
            source_digests.append(upstream.sources[key].sha256)
            upstream.verify(key)
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if matcher_id is MatcherId.ADAFACE_IR101:
            net = upstream.load_module("adaface_net")
            model = net.build_model("ir_101")
            if not isinstance(state, dict) or "state_dict" not in state:
                raise MatcherRuntimeError("AdaFace checkpoint does not carry a state_dict")
            state_dict = {key[len("model.") :]: value for key, value in state["state_dict"].items() if key.startswith("model.")}
        elif matcher_id is MatcherId.ELASTICFACE_ARC_R100:
            iresnet = upstream.load_module("insightface_arcface_torch_iresnet")
            model = iresnet.iresnet100(num_features=512)
            state_dict = state
        else:
            vit = upstream.load_module("transface_vit")
            # Constructor arguments from TransFace backbones/__init__.py @ fb6cd56d, name vit_s_dp005_mask_0.
            model = vit.VisionTransformer(
                img_size=112, patch_size=9, num_classes=512, embed_dim=512, depth=12,
                num_heads=8, drop_path_rate=0.05, norm_layer="ln", mask_ratio=0.0,
            )
            state_dict = state
        result = model.load_state_dict(state_dict, strict=True)
        if getattr(result, "missing_keys", []) or getattr(result, "unexpected_keys", []):
            raise MatcherRuntimeError(f"{matcher_id.value}: strict load reported {result}")
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)
        self.model = model.to(self.device)
        self.binding = MatcherBinding(
            matcher_id=matcher_id,
            transform_id=TRANSFORM_IDS[matcher_id],
            checkpoint_path=str(checkpoint),
            checkpoint_sha256=digest,
            checkpoint_size=checkpoint.stat().st_size,
            architecture_source_sha256=tuple(source_digests),
            parameter_count=sum(int(p.numel()) for p in model.parameters()),
            device=self.device,
            torch_version=torch.__version__,
        )

    def _to_tensor(self, crops_bgr: np.ndarray) -> torch.Tensor:
        if crops_bgr.ndim != 4 or crops_bgr.shape[1:] != (112, 112, 3) or crops_bgr.dtype != np.uint8:
            raise MatcherRuntimeError("matcher input must be N×112×112×3 uint8 BGR crops")
        array = crops_bgr
        if COLOR_ORDER[self.matcher_id] == "RGB":
            array = array[:, :, :, ::-1]
        array = np.ascontiguousarray(array).astype(np.float32)
        array = (array / 255.0 - 0.5) / 0.5  # [-1, 1]
        tensor = torch.from_numpy(array).permute(0, 3, 1, 2).contiguous()
        return tensor.to(self.device)

    @torch.no_grad()
    def embed(self, crops_bgr: np.ndarray, batch_size: int = 64) -> np.ndarray:
        outputs: list[np.ndarray] = []
        for start in range(0, crops_bgr.shape[0], batch_size):
            batch = self._to_tensor(crops_bgr[start : start + batch_size])
            output = self.model(batch)
            if isinstance(output, (tuple, list)):
                output = output[0]  # AdaFace returns (embedding, norm)
            embedding = output.float().cpu().numpy()
            outputs.append(embedding)
        embeddings = np.concatenate(outputs, axis=0) if outputs else np.zeros((0, 512), dtype=np.float32)
        if embeddings.shape[1] != 512:
            raise MatcherRuntimeError(f"{self.matcher_id.value} produced {embeddings.shape[1]}-dimensional embeddings")
        if not np.all(np.isfinite(embeddings)):
            raise MatcherRuntimeError(f"{self.matcher_id.value} produced non-finite embeddings")
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise MatcherRuntimeError(f"{self.matcher_id.value} produced a zero embedding")
        return (embeddings / norms).astype(np.float32)


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return np.sum(left * right, axis=1)
