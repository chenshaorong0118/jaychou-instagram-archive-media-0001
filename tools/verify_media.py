#!/usr/bin/env python3
"""Verify a generated media shard using only Python's standard library."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


SHA_RE = re.compile(r"^[a-f0-9]{64}$")
ENTRY_RE = re.compile(
    r"^(posts|stories)/\d{4}/\d{2}/\d{2}/\d{8}T\d{6}\+0800_"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)
ITEM_KEYS = {
    "schema_version",
    "pk",
    "item_type",
    "published_at_utc",
    "published_at_taipei",
    "caption",
    "text",
    "shared_post",
    "music",
    "has_image",
    "has_video",
    "has_audio",
    "media",
}
ASSET_TYPES = {"image", "video", "audio", "poster", "thumbnail"}
FORBIDDEN_KEYS = {
    "source_url",
    "captured_at",
    "display_timezone",
    "visible_text",
    "origin",
    "derived_from",
    "run_id",
    "absolute_path",
}


def fail(message: str) -> None:
    raise ValueError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe(root: Path, relative: str) -> Path:
    if "\\" in relative or relative.startswith("/") or ".." in relative.split("/"):
        fail(f"unsafe path: {relative}")
    result = (root / relative).resolve()
    if root != result and root not in result.parents:
        fail(f"path escapes root: {relative}")
    return result


def walk_keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).casefold()
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def verify_metadata(root: Path, row: dict) -> int:
    relative = row.get("path")
    if not isinstance(relative, str) or not ENTRY_RE.fullmatch(relative):
        fail(f"invalid manifest path: {relative!r}")
    metadata_path = safe(root, f"{relative}/metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 2 or metadata.get("pk") != row.get("pk"):
        fail(f"metadata identity mismatch: {relative}")
    if set(metadata) != ITEM_KEYS:
        fail(f"metadata fields are not minimal: {relative}")
    forbidden = FORBIDDEN_KEYS.intersection(walk_keys(metadata))
    if forbidden:
        fail(f"forbidden metadata key: {relative}:{sorted(forbidden)[0]}")
    media = metadata.get("media")
    if not isinstance(media, list) or len(media) != row.get("media_count"):
        fail(f"media count mismatch: {relative}")
    checked = 0
    expected_files = {"metadata.json"}
    for expected_index, position in enumerate(media, 1):
        if position.get("index") != expected_index:
            fail(f"noncontiguous media index: {relative}")
        kind = position.get("kind")
        if kind not in {"image", "video", "image_with_audio"}:
            fail(f"invalid presentation kind: {relative}")
        assets = position.get("assets")
        if not isinstance(assets, list) or not assets:
            fail(f"assets missing: {relative}")
        asset_types = {asset.get("type") for asset in assets}
        if not asset_types.issubset(ASSET_TYPES):
            fail(f"invalid asset type: {relative}")
        if len([asset for asset in assets if asset.get("type") == "thumbnail"]) != 1:
            fail(f"thumbnail missing/duplicate: {relative}")
        if kind == "image_with_audio" and not {
            "image",
            "video",
            "thumbnail",
        }.issubset(asset_types):
            fail(f"image_with_audio pair invalid: {relative}")
        for asset in assets:
            filename = asset.get("filename")
            expected_sha = asset.get("sha256")
            expected_bytes = asset.get("bytes")
            if not isinstance(filename, str) or not SHA_RE.fullmatch(str(expected_sha)):
                fail(f"asset metadata invalid: {relative}")
            asset_path = safe(root, f"{relative}/{filename}")
            expected_files.add(filename)
            if asset_path.stat().st_size != expected_bytes or sha256(asset_path) != expected_sha:
                fail(f"asset integrity mismatch: {relative}/{filename}")
            if asset.get("type") != "audio" and (
                not isinstance(asset.get("width"), int)
                or not isinstance(asset.get("height"), int)
            ):
                fail(f"visual dimensions missing: {relative}/{filename}")
            if asset.get("type") == "thumbnail":
                if (
                    asset.get("mime_type") != "image/webp"
                    or max(int(asset.get("width") or 0), int(asset.get("height") or 0))
                    > 640
                    or not re.match(
                        b"^RIFF....WEBP",
                        asset_path.read_bytes()[:12],
                        re.DOTALL,
                    )
                ):
                    fail(f"invalid WebP thumbnail: {relative}/{filename}")
            checked += 1
    actual_files = {
        path.name for path in metadata_path.parent.iterdir() if path.is_file()
    }
    if actual_files != expected_files:
        fail(f"undeclared item file: {relative}")
    for field in ("has_image", "has_video", "has_audio"):
        if not isinstance(metadata.get(field), bool):
            fail(f"media flag missing: {relative}:{field}")
    return checked


def verify_checksums(root: Path) -> None:
    expected_files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts and path.name != "SHA256SUMS"
    )
    rows = root.joinpath("SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed: list[str] = []
    for row in rows:
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", row)
        if not match:
            fail("invalid SHA256SUMS row")
        expected_sha, relative = match.groups()
        listed.append(relative)
        if sha256(safe(root, relative)) != expected_sha:
            fail(f"SHA256SUMS mismatch: {relative}")
    if listed != expected_files:
        fail("SHA256SUMS file list/order mismatch")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    manifest_rows = [
        json.loads(line)
        for line in root.joinpath("manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    keys = [(row.get("published_at_taipei"), row.get("pk")) for row in manifest_rows]
    if keys != sorted(keys) or len({row.get("pk") for row in manifest_rows}) != len(manifest_rows):
        fail("manifest order or PK uniqueness invalid")
    assets = sum(verify_metadata(root, row) for row in manifest_rows)
    verify_checksums(root)
    print(json.dumps({"ok": True, "items": len(manifest_rows), "assets": assets}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
