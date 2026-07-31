#!/usr/bin/env python3
"""Regenerate the deterministic repository checksum list."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


root = Path(__file__).resolve().parents[1]
paths = sorted(
    (
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and path.name != "SHA256SUMS"
    ),
    key=lambda path: path.relative_to(root).as_posix(),
)
root.joinpath("SHA256SUMS").write_text(
    "".join(
        f"{sha256(path)}  {path.relative_to(root).as_posix()}\n"
        for path in paths
    ),
    encoding="utf-8",
    newline="\n",
)
