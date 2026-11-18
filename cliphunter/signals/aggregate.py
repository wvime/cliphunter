"""Weighted z-scores, hysteresis, and a 15–45 s clip around the peak."""

from __future__ import annotations

from dataclasses import dataclass, field

from cliphunter.index import Segment, SegmentIndex
from cliphunter.timeutil import parse_iso

WEIGHT_ALIASES = {
    "chat": "chat_velocity",
    "chat_velocity": "chat_velocity",
    "emotes": "emote_burst",
    "emote_burst": "emote_burst",
    "audio": "audio_energy",
    "audio_energy": "audio_energy",
    "scene": "scene_change",
    "scene_change": "scene_change",
    "viewers": "viewer_delta",
    "viewer_delta": "viewer_delta",
}

DEFAULT_WEIGHTS = {
    "chat_velocity": 0.35,
    "emote_burst": 0.25,
    "audio_energy": 0.30,
    "scene_change": 0.05,
    "viewer_delta": 0.05,
}


def canonical_weights(raw: dict[str, float] | None) -> dict[str, float]:
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    merged = dict(DEFAULT_WEIGHTS)
    for key, value in raw.items():
        name = WEIGHT_ALIASES.get(key, key)
        merged[name] = float(value)
    return merged


def channel_threshold(raw: dict[str, float] | None, default: float = 2.5) -> float:
    if not raw:
        return default
    for key in ("signal_score", "clip_score"):
        if key in raw:
            return float(raw[key])
    return default


def channel_cooldown(raw: dict[str, float] | None, default: float = 45.0) -> float:
    if not raw:
        return default
    if "cooldown_s" in raw:
        return float(raw["cooldown_s"])
    return default


@dataclass
class ScoreTick:
    score: float
    parts: dict[str, dict[str, float]]
    degraded: list[str]


def combine(parts: dict[str, dict[str, float]], degraded: list[str]) -> ScoreTick:
    """Renormalize over the signals that actually returned a z-score."""

    usable = {name: item for name, item in parts.items() if item.get("weight", 0) > 0}
    weight_sum = sum(item["weight"] for item in usable.values())
    if weight_sum <= 0:
        return ScoreTick(score=0.0, parts=parts, degraded=degraded)
    score = sum(item["weight"] / weight_sum * item["z"] for item in usable.values())
    return ScoreTick(score=score, parts=parts, degraded=list(degraded))


@dataclass
class MomentTracker:
    open: bool = False
    samples: list[tuple[float, float]] = field(default_factory=list)
    peak: tuple[float, float] | None = None
    started: float | None = None

    def push(self, t: float, score: float, *, threshold: float, release: float, max_len: float) -> str:
        if not self.open:
            if score >= threshold:
                self.open = True
                self.samples = [(t, score)]
                self.peak = (t, score)
                self.started = t
                return "rising"
            return "idle"
        self.samples.append((t, score))
        if self.peak is None or score >= self.peak[1]:
            self.peak = (t, score)
        if score < release or (self.started is not None and t - self.started >= max_len):
            return "fire"
        return "rising"

    def reset(self) -> None:
        self.open = False
        self.samples.clear()
        self.peak = None
        self.started = None


def choose_bounds(
    samples: list[tuple[float, float]],
    *,
    min_seconds: float,
    max_seconds: float,
    target_seconds: float,
    pre_roll: float,
) -> tuple[float, float, float]:
    """Return ``(start, end, peak)`` clamped to min/max around the peak."""

    if not samples:
        raise ValueError("no samples")
    peak_t, _peak_score = max(samples, key=lambda item: item[1])
    rise = samples[0][0]
    fall = samples[-1][0]
    start = min(rise, peak_t - pre_roll)
    end = max(fall, start + target_seconds)
    duration = end - start
    if duration < min_seconds:
        extra = min_seconds - duration
        start -= extra * 0.65
        end += extra * 0.35
    if end - start > max_seconds:
        start = peak_t - min(pre_roll + 4.0, max_seconds * 0.4)
        end = start + max_seconds
    if end < start:
        end = start + min_seconds
    return start, end, peak_t


def segments_for_window(
    index: SegmentIndex,
    start: float,
    end: float,
    peak: float,
    *,
    min_seconds: float,
    max_seconds: float,
) -> list[Segment]:
    """Pick whole segments covering ``[start, end]``, then clamp the duration."""

    if not index.segments:
        return []
    chosen = [index.segments.index(segment) for segment in index.segments if _overlaps(segment, start, end)]
    if not chosen:
        nearest = min(range(len(index.segments)), key=lambda pos: abs(_mid(index.segments[pos]) - peak))
        chosen = [nearest]
    while _duration(index, chosen) < min_seconds:
        if chosen[0] > 0:
            chosen.insert(0, chosen[0] - 1)
            continue
        if chosen[-1] + 1 < len(index.segments):
            chosen.append(chosen[-1] + 1)
            continue
        break
    while _duration(index, chosen) > max_seconds and len(chosen) > 1:
        left = abs(_mid(index.segments[chosen[0]]) - peak)
        right = abs(_mid(index.segments[chosen[-1]]) - peak)
        if left >= right:
            chosen.pop(0)
        else:
            chosen.pop()
    return [index.segments[pos] for pos in chosen]


def _overlaps(segment: Segment, start: float, end: float) -> bool:
    wall_start = parse_iso(segment.wall_start).timestamp()
    wall_end = parse_iso(segment.wall_end).timestamp()
    return wall_end > start and wall_start < end


def _mid(segment: Segment) -> float:
    wall_start = parse_iso(segment.wall_start).timestamp()
    wall_end = parse_iso(segment.wall_end).timestamp()
    return (wall_start + wall_end) / 2


def _duration(index: SegmentIndex, positions: list[int]) -> float:
    return sum(index.segments[pos].duration for pos in positions)
