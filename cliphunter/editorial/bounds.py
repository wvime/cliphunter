"""Snap a clip to transcript lines: setup sentence through the reaction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    start: float
    end: float
    text: str


def clip_bounds(
    lines: list[Line],
    setup: float,
    payoff: float,
    end: float,
    *,
    min_seconds: float = 15,
    max_seconds: float = 60,
    story_max_seconds: float = 120,
    storyline: bool = False,
) -> tuple[float, float, float]:
    """Return ``(start, end, payoff)`` on line edges.

    The start is the beginning of the setup sentence. The end is the end of
    the line that holds the reaction. A storyline may run past ``max_seconds``
    up to ``story_max_seconds``.
    """

    if not lines:
        start = setup
        stop = max(end, setup + min_seconds)
        cap = story_max_seconds if storyline else max_seconds
        if stop - start > cap:
            stop = start + cap
        return start, stop, payoff
    ordered = sorted(lines, key=lambda line: line.start)
    start_line = _covering(ordered, setup) or ordered[0]
    end_line = _covering(ordered, end) or ordered[-1]
    start = start_line.start
    stop = max(end_line.end, start)
    if payoff < start:
        payoff = start
    cap = story_max_seconds if storyline else max_seconds
    if stop - start < min_seconds:
        stop, start = _extend(ordered, start, stop, min_seconds)
    if stop - start < min_seconds:
        # More video after the last line, not invented dialogue.
        stop += min_seconds - (stop - start)
    if stop - start > cap:
        stop = start + cap
        stop = _end_of_line_at_or_before(ordered, stop, start)
    if stop <= start:
        stop = start + min(min_seconds, cap)
    return start, stop, payoff


def _covering(lines: list[Line], t: float) -> Line | None:
    exact = [line for line in lines if line.start <= t <= line.end]
    if exact:
        return max(exact, key=lambda line: line.start)
    earlier = [line for line in lines if line.start <= t]
    return earlier[-1] if earlier else None


def _extend(lines: list[Line], start: float, stop: float, minimum: float) -> tuple[float, float]:
    """Add following lines only. The setup sentence stays the start."""

    guard = 0
    while stop - start < minimum and guard < len(lines) + 2:
        guard += 1
        nxt = next((line for line in lines if line.start >= stop - 1e-6 and line.end > stop), None)
        if nxt is None:
            break
        stop = nxt.end
    return stop, start


def _end_of_line_at_or_before(lines: list[Line], t: float, start: float) -> float:
    candidates = [line.end for line in lines if start < line.end <= t + 1e-6]
    if not candidates:
        return t
    return max(candidates)
