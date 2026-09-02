#!/usr/bin/env python3
"""Resolve real browser-gate inputs from the reviewed resource registry."""

from __future__ import annotations

import json
import os
from pathlib import Path


LFW_RESOURCE_ID = "lfw-funneled-local-candidate"


def lfw_tree_root(repository_root: Path) -> Path:
    registry_path = Path(
        os.environ.get(
            "QUALSIGHT_REGISTRY_PATH",
            str(repository_root / "resources" / "resource-registry.json"),
        )
    ).resolve(strict=True)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    resources = [
        resource
        for resource in registry.get("resources", [])
        if resource.get("resource_id") == LFW_RESOURCE_ID
    ]
    if len(resources) != 1:
        raise RuntimeError("reviewed registry does not contain exactly one LFW resource")
    checks = [
        check
        for check in resources[0].get("checks", [])
        if check.get("type") == "tree_manifest"
    ]
    if len(checks) != 1 or not isinstance(checks[0].get("root"), str):
        raise RuntimeError("reviewed LFW resource does not contain one tree-manifest root")
    root = Path(checks[0]["root"])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise RuntimeError("reviewed LFW tree root is unavailable or unsafe")
    return root
