#!/usr/bin/env python3
"""Generate required WebP thumbnails locally; never run this tool in CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


WEBP_RE = re.compile(b"^RIFF....WEBP", re.DOTALL)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or f"command failed: {args[0]}")
    return completed.stdout


def probe_dimensions(ffprobe: Path, path: Path) -> tuple[int, int]:
    payload = json.loads(
        run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams = payload.get("streams", [])
    if not streams:
        raise RuntimeError(f"visual stream missing: {path}")
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    if width < 1 or height < 1:
        raise RuntimeError(f"visual dimensions missing: {path}")
    return width, height


def has_audio(ffprobe: Path, path: Path) -> bool:
    return bool(
        run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ]
        ).strip()
    )


def generate_thumbnail(
    ffmpeg: Path,
    ffprobe: Path,
    source: Path,
    destination: Path,
) -> tuple[int, int]:
    run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-an",
            "-vf",
            (
                "scale=w='min(640,iw)':h='min(640,ih)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
            ),
            "-map_metadata",
            "-1",
            "-c:v",
            "libwebp",
            "-quality",
            "82",
            "-compression_level",
            "6",
            "-preset",
            "picture",
            str(destination),
        ]
    )
    if not destination.is_file() or destination.stat().st_size <= 12:
        raise RuntimeError(f"thumbnail generation failed: {source}")
    if not WEBP_RE.match(destination.read_bytes()[:12]):
        raise RuntimeError(f"not WebP: {destination}")
    width, height = probe_dimensions(ffprobe, destination)
    if max(width, height) > 640:
        raise RuntimeError(f"thumbnail too large: {destination}")
    return width, height


def thumbnail_source(position: dict[str, Any]) -> dict[str, Any]:
    assets = position["assets"]
    for role in ("image", "primary_video", "playable_video"):
        for asset in assets:
            if asset.get("role") == role:
                return asset
    raise RuntimeError("thumbnail source missing")


def update_metadata(
    metadata_path: Path,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    check: bool,
) -> tuple[bool, int]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    changed = False
    thumbnails = 0
    item_has_audio = False
    item_has_image = False
    item_has_video = False
    for position in metadata["media"]:
        assets = position["assets"]
        item_has_image = item_has_image or any(
            asset.get("role") == "image" for asset in assets
        )
        video_assets = [
            asset
            for asset in assets
            if str(asset.get("mime_type", "")).startswith("video/")
        ]
        item_has_video = item_has_video or bool(video_assets)
        for asset in video_assets:
            item_has_audio = item_has_audio or has_audio(
                ffprobe, metadata_path.parent / str(asset["filename"])
            )
        existing = [
            asset for asset in assets if asset.get("role") == "thumbnail"
        ]
        if len(existing) > 1:
            raise RuntimeError(f"duplicate thumbnails: {metadata_path}")
        source = thumbnail_source(position)
        name = f"{int(position['media_index']):02d}-thumbnail.webp"
        destination = metadata_path.parent / name
        if existing:
            thumbnail = existing[0]
            if (
                thumbnail.get("filename") != name
                or not destination.is_file()
                or destination.stat().st_size != thumbnail.get("bytes")
                or sha256(destination) != thumbnail.get("sha256")
                or not WEBP_RE.match(destination.read_bytes()[:12])
            ):
                raise RuntimeError(f"invalid existing thumbnail: {destination}")
        else:
            if check:
                raise RuntimeError(f"thumbnail missing: {metadata_path}")
            width, height = generate_thumbnail(
                ffmpeg, ffprobe, metadata_path.parent / source["filename"], destination
            )
            assets.append(
                {
                    "role": "thumbnail",
                    "filename": name,
                    "origin": "derived_thumbnail",
                    "mime_type": "image/webp",
                    "width": width,
                    "height": height,
                    "bytes": destination.stat().st_size,
                    "sha256": sha256(destination),
                    "derived_from": [source["sha256"]],
                }
            )
            changed = True
        thumbnails += 1
    flags = {
        "has_image": item_has_image,
        "has_video": item_has_video,
        "has_audio": item_has_audio,
    }
    for key, value in flags.items():
        if metadata.get(key) != value:
            if check:
                raise RuntimeError(f"{key} is stale: {metadata_path}")
            metadata[key] = value
            changed = True
    if changed:
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return changed, thumbnails


def write_checksums(root: Path) -> None:
    rows: list[str] = []
    for path in sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.name != "SHA256SUMS"
        ),
        key=lambda value: value.relative_to(root).as_posix(),
    ):
        rows.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    root.joinpath("SHA256SUMS").write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    metadata_files = sorted(root.glob("posts/**/metadata.json")) + sorted(
        root.glob("stories/**/metadata.json")
    )
    changed = 0
    thumbnails = 0
    for metadata_path in metadata_files:
        was_changed, count = update_metadata(
            metadata_path,
            ffmpeg=Path(args.ffmpeg).resolve(),
            ffprobe=Path(args.ffprobe).resolve(),
            check=args.check,
        )
        changed += int(was_changed)
        thumbnails += count
    if not args.check:
        write_checksums(root)
    print(
        json.dumps(
            {
                "ok": True,
                "items": len(metadata_files),
                "changed": changed,
                "thumbnails": thumbnails,
                "check": args.check,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
