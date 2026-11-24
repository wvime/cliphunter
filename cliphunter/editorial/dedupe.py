"""One moment, one clip, even if another mode already sent it."""

from __future__ import annotations

import json

from cliphunter.timeutil import parse_iso


def center_distance(start: float, end: float, moment: float) -> float:
    return abs(((start + end) / 2) - moment)


def merge_overlapping(existing, new):
    """Mean the scores. Keep the bounds of the packet that sits closer to its moment."""

    from cliphunter.editorial.compare import Pick

    score = existing.score
    if existing.score is not None and new.score is not None:
        score = round((existing.score + new.score) / 2, 1)
    elif new.score is not None:
        score = new.score
    moment_existing = float(existing.extra.get("moment", (existing.start + existing.end) / 2))
    moment_new = float(new.extra.get("moment", (new.start + new.end) / 2))
    best = existing if center_distance(existing.start, existing.end, moment_existing) <= center_distance(new.start, new.end, moment_new) else new
    return Pick(
        start=best.start,
        end=best.end,
        score=score,
        trigger=best.trigger,
        category=best.category or existing.category,
        title=best.title or existing.title,
        tier=best.tier,
        why=best.why or existing.why,
        accepted=bool(existing.accepted or new.accepted),
        participants=best.participants or existing.participants,
        extra=dict(best.extra),
    )


def ranges_overlap(start: float, end: float, other_start: float, other_end: float, *, ratio: float = 0.3) -> bool:
    overlap = min(end, other_end) - max(start, other_start)
    shorter = min(end - start, other_end - other_start)
    return shorter > 0 and overlap / shorter >= ratio


def _span(attribution: dict) -> tuple[float, float] | None:
    raw_start = attribution.get("clip_start")
    raw_end = attribution.get("clip_end")
    if not raw_start or not raw_end:
        return None
    try:
        return parse_iso(str(raw_start)).timestamp(), parse_iso(str(raw_end)).timestamp()
    except ValueError:
        return None


async def overlaps_saved(repo, channel_id: int, start: float, end: float) -> bool:
    if not channel_id:
        return False
    try:
        rows = await repo.list_clips(channel_id)
    except Exception:
        return False
    for row in rows:
        if row.get("status") in {"failed", "rejected"}:
            continue
        try:
            attribution = json.loads(row.get("attribution_json") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(attribution, dict):
            continue
        span = _span(attribution)
        if span and ranges_overlap(start, end, span[0], span[1]):
            return True
    return False
