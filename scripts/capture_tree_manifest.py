#!/usr/bin/env python3
"""Capture a deterministic byte manifest for an existing real file tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(root: Path, pattern: str, output: Path) -> tuple[int, int, str]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"manifest root is not a directory: {root}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite manifest: {output}")
    paths = sorted(
        (path for path in root.rglob(pattern) if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    if not paths:
        raise ValueError(f"no files matched {pattern!r} under {root}")

    records: list[str] = []
    tree_digest = hashlib.sha256()
    total_bytes = 0
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"manifest input must not be a symlink: {path}")
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError(f"manifest input escapes root: {path}") from error
        digest = _sha256(resolved)
        size_bytes = resolved.stat().st_size
        total_bytes += size_bytes
        tree_digest.update(f"{digest}  {relative}\n".encode("utf-8"))
        records.append(
            json.dumps(
                {
                    "path": relative,
                    "sha256": digest,
                    "size_bytes": size_bytes,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    if partial.exists():
        raise FileExistsError(f"refusing to overwrite partial manifest: {partial}")
    published = False
    try:
        with partial.open("x", encoding="utf-8", newline="\n") as handle:
            handle.writelines(records)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(partial, output)
        published = True
        partial.unlink()
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if not published and partial.exists():
            partial.unlink()
    return len(paths), total_bytes, tree_digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    count, total_bytes, tree_sha256 = capture(
        args.root,
        args.pattern,
        args.output,
    )
    print(
        f"TREE_MANIFEST_CAPTURED count={count} total_bytes={total_bytes} "
        f"tree_sha256={tree_sha256} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
