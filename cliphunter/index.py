"""On-disk segment index shared by ``run`` and ``clip``."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cliphunter.atomic import atomic_write_json
from cliphunter.timeutil import parse_iso, utc_iso

INDEX_FILENAME = "index.json"


@dataclass
class Segment:
    file: str
    duration: float
    wall_start: str
    wall_end: str
    size_bytes: int = 0
    session_id: int | None = None
    stream_url: str | None = None
    stream_title: str | None = None
    streamer: str | None = None
    platform: str | None = None

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "duration": round(self.duration, 3),
            "wall_start": self.wall_start,
            "wall_end": self.wall_end,
            "size_bytes": self.size_bytes,
            "session_id": self.session_id,
            "stream_url": self.stream_url,
            "stream_title": self.stream_title,
            "streamer": self.streamer,
            "platform": self.platform,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Segment:
        return cls(
            file=str(data["file"]),
            duration=float(data["duration"]),
            wall_start=str(data["wall_start"]),
            wall_end=str(data["wall_end"]),
            size_bytes=int(data.get("size_bytes") or 0),
            session_id=data.get("session_id"),
            stream_url=data.get("stream_url"),
            stream_title=data.get("stream_title"),
            streamer=data.get("streamer"),
            platform=data.get("platform"),
        )


@dataclass
class SegmentIndex:
    channel_key: str
    platform: str
    directory: Path
    session: dict = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    version: int = 1

    def add(self, segment: Segment) -> None:
        self.segments.append(segment)

    def select_last(self, seconds: float) -> list[Segment]:
        """Whole segments from the end until their durations cover ``seconds``.

        The result can be longer than requested by at most one segment.
        That is the keyframe/segment precision of a copy-only cut.
        """

        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        chosen: list[Segment] = []
        total = 0.0
        for segment in reversed(self.segments):
            chosen.append(segment)
            total += segment.duration
            if total >= seconds - 1e-6:
                break
        chosen.reverse()
        return chosen

    def drop_older_than(self, cutoff: datetime) -> list[Segment]:
        kept: list[Segment] = []
        removed: list[Segment] = []
        for segment in self.segments:
            if parse_iso(segment.wall_end) < cutoff:
                removed.append(segment)
            else:
                kept.append(segment)
        self.segments = kept
        return removed

    @property
    def path(self) -> Path:
        return self.directory / INDEX_FILENAME

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "channel_key": self.channel_key,
            "platform": self.platform,
            "session": self.session,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    def save(self) -> None:
        atomic_write_json(self.path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> SegmentIndex:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            channel_key=str(data.get("channel_key") or path.parent.name),
            platform=str(data.get("platform") or ""),
            directory=path.parent,
            session=dict(data.get("session") or {}),
            segments=[Segment.from_dict(item) for item in data.get("segments") or []],
            version=int(data.get("version") or 1),
        )

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        channel_key: str,
        platform: str,
        session: dict,
    ) -> SegmentIndex:
        path = directory / INDEX_FILENAME
        if path.exists():
            index = cls.load(path)
            index.directory = directory
            index.channel_key = channel_key
            index.platform = platform
            index.session = dict(session)
            return index
        return cls(
            channel_key=channel_key,
            platform=platform,
            directory=directory,
            session=dict(session),
        )


def parse_segment_csv(text: str) -> list[tuple[str, float, float]]:
    """Parse ffmpeg's segment list CSV: filename,start,end."""

    rows: list[tuple[str, float, float]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = next(csv.reader(io.StringIO(stripped)))
        if len(parts) < 3:
            continue
        try:
            start = float(parts[1])
            end = float(parts[2])
        except ValueError:
            continue
        rows.append((parts[0], start, end))
    return rows


def apply_retention(index: SegmentIndex, *, now: datetime, buffer_minutes: int) -> list[Path]:
    """Drop segments older than the rolling window and delete their files.

    The segment ffmpeg is still writing is not in the index and its mtime is
    fresh, so it is left alone. Orphan ``.ts`` / ``g*.csv`` files older than
    the window are removed too.
    """

    cutoff = now - timedelta(minutes=buffer_minutes)
    removed = index.drop_older_than(cutoff)
    deleted: list[Path] = []
    for segment in removed:
        path = index.directory / segment.file
        if _unlink(path):
            deleted.append(path)
    known = {segment.file for segment in index.segments}
    if index.directory.exists():
        candidates = list(index.directory.glob("*.ts")) + list(index.directory.glob("g*.csv"))
        for path in candidates:
            if path.name in known:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
            except OSError:
                continue
            if modified < cutoff and _unlink(path):
                deleted.append(path)
    return deleted


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def wall_window(duration: float, *, end: datetime | None = None) -> tuple[str, str]:
    from cliphunter.timeutil import utc_now

    wall_end = end or utc_now()
    wall_start = wall_end - timedelta(seconds=duration)
    return utc_iso(wall_start), utc_iso(wall_end)
