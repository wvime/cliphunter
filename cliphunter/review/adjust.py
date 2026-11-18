"""Shift a copy-only clip window by about five seconds, on whole segments."""

from __future__ import annotations

from cliphunter.clip import ClipError
from cliphunter.index import Segment, SegmentIndex


def shift_window(
    index: SegmentIndex,
    selected_files: list[str],
    *,
    edge: str,
    delta_seconds: float,
) -> list[Segment]:
    """Move the start or the end.

    Negative ``delta_seconds`` on ``inicio`` starts earlier. Positive starts later.
    Negative on ``fin`` ends earlier. Positive ends later.
    Segments are only added or removed whole, so the change can overshoot 5 s.
    """

    if edge not in {"inicio", "fin"}:
        raise ClipError(f"borde desconocido: {edge}")
    if not index.segments:
        raise ClipError("el buffer no tiene segmentos")
    wanted = set(selected_files)
    positions = {segment.file: pos for pos, segment in enumerate(index.segments)}
    chosen = [segment for segment in index.segments if segment.file in wanted]
    if not chosen:
        raise ClipError("el buffer ya no tiene los segmentos de este clip")
    start = positions[chosen[0].file]
    end = positions[chosen[-1].file]
    target = abs(delta_seconds)
    if edge == "inicio" and delta_seconds < 0:
        gained = 0.0
        cursor = start - 1
        while cursor >= 0 and gained < target - 1e-6:
            gained += index.segments[cursor].duration
            cursor -= 1
        start = cursor + 1
    elif edge == "inicio" and delta_seconds > 0:
        lost = 0.0
        cursor = start
        while cursor < end and lost < target - 1e-6:
            lost += index.segments[cursor].duration
            cursor += 1
        start = cursor
    elif edge == "fin" and delta_seconds < 0:
        lost = 0.0
        cursor = end
        while cursor > start and lost < target - 1e-6:
            lost += index.segments[cursor].duration
            cursor -= 1
        end = cursor
    elif edge == "fin" and delta_seconds > 0:
        gained = 0.0
        cursor = end + 1
        while cursor < len(index.segments) and gained < target - 1e-6:
            gained += index.segments[cursor].duration
            cursor += 1
        end = cursor - 1
    if start > end:
        raise ClipError("el ajuste dejaría el clip vacío")
    return list(index.segments[start : end + 1])
