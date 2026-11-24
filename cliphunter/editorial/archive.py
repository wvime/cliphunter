"""Keep the segments a pending clip needs after the rolling buffer deletes them."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cliphunter.index import Segment, SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.timeutil import parse_iso


def archive_root(buffer_dir: Path) -> Path:
    return buffer_dir.parent / "archivo_revision"


def shifted_bounds(clip_start: str, clip_end: str, edge: str, delta: float) -> tuple[float, float]:
    start = parse_iso(clip_start).timestamp()
    end = parse_iso(clip_end).timestamp()
    if edge == "inicio":
        start += delta
    else:
        end += delta
    if end <= start:
        end = start + 1.0
    return start, end


def retain_clip(
    *,
    buffer_dir: Path,
    clip_id: int,
    index_path: Path,
    files: list[str],
    margin_seconds: float,
) -> Path | None:
    if not index_path.exists() or not files:
        return None
    index = SegmentIndex.load(index_path)
    wanted = set(files)
    positions = [pos for pos, segment in enumerate(index.segments) if segment.file in wanted]
    if not positions:
        return None
    lo, hi = min(positions), max(positions)
    extra = 0.0
    while lo > 0 and extra < margin_seconds:
        lo -= 1
        extra += index.segments[lo].duration
    extra = 0.0
    while hi + 1 < len(index.segments) and extra < margin_seconds:
        hi += 1
        extra += index.segments[hi].duration
    dest = archive_root(buffer_dir) / str(clip_id)
    dest.mkdir(parents=True, exist_ok=True)
    kept: list[Segment] = []
    for segment in index.segments[lo : hi + 1]:
        source = index.directory / segment.file
        if not source.exists():
            continue
        target = dest / Path(segment.file).name
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        kept.append(
            Segment(
                file=target.name,
                duration=segment.duration,
                wall_start=segment.wall_start,
                wall_end=segment.wall_end,
                size_bytes=target.stat().st_size,
                streamer=segment.streamer,
                platform=segment.platform,
                stream_url=segment.stream_url,
                stream_title=segment.stream_title,
            )
        )
    if not kept:
        return None
    stored = SegmentIndex(
        channel_key=index.channel_key,
        platform=index.platform,
        directory=dest,
        session=dict(index.session),
        segments=kept,
    )
    stored.save()
    log_event("INFO", "review.archived", clip_id=clip_id, segments=len(kept))
    return stored.path


def archive_index(buffer_dir: Path, clip_id: int) -> Path | None:
    path = archive_root(buffer_dir) / str(clip_id) / "index.json"
    return path if path.exists() else None


def drop_archive(buffer_dir: Path, clip_id: int) -> None:
    folder = archive_root(buffer_dir) / str(clip_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
