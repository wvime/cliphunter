"""Snap a clip to transcript lines: setup sentence through the reaction."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Line:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


TOPIC_MARKERS = (
    "bienvenido",
    "bienvenida",
    "aqui tenemos",
    "aquí tenemos",
    "llego",
    "llegó",
    "el siguiente",
    "con nosotros",
)
LAUGHTER = ("jaja", "jeje", "jiji", "risa")


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
    payoff_quote: str | None = None,
    lead_in: float = 0.3,
    tail: float = 0.5,
    reaction_cap: float = 4.0,
) -> tuple[float, float, float]:
    """Return ``(start, end, payoff)`` tight around the spoken words.

    The start is at most ``lead_in`` before the first spoken word of the
    setup sentence. Segment starts are ignored when word times exist, because
    those starts include the pause before anyone talks. The payoff quote is
    kept whole. The end is about ``tail`` after the last reaction word, and
    trailing talk is capped unless the laugh is still going.
    """

    cap = story_max_seconds if storyline else max_seconds
    if not lines:
        start = setup - lead_in
        stop = max(end, setup) + tail
        if stop - start > cap:
            start = stop - cap
        return start, stop, payoff
    ordered = sorted(lines, key=lambda line: line.start)
    setup_line = _covering(ordered, setup) or ordered[0]
    payoff_line = _quote_line(ordered, payoff_quote) or _covering(ordered, payoff) or setup_line
    end_line = _covering(ordered, end) or payoff_line
    if end_line.end < payoff_line.end:
        end_line = payoff_line
    start = _start_at_first_word(setup_line, lead_in)
    reaction_end = _last_word_end(end_line) + tail
    stop = max(reaction_end, _last_word_end(payoff_line) + tail)
    stop = _cap_trailing(ordered, end_line, stop, reaction_cap, tail)
    topic = _topic_cut(ordered, payoff_line.end)
    if topic is not None:
        stop = min(stop, topic)
    stop = max(stop, _last_word_end(payoff_line) + tail)
    if stop - start < min_seconds:
        start = _extend_backward(ordered, start, stop, min_seconds, lead_in)
    anchor = _start_at_first_word(setup_line, lead_in)
    if start < anchor:
        start = anchor
    if stop - start > cap:
        start = max(anchor, stop - cap)
        if stop - start > cap:
            stop = start + cap
    if stop <= start:
        stop = start + min(min_seconds, cap)
    return start, stop, payoff_line.start


def _last_word_end(line: Line) -> float:
    if line.words:
        return max(word.end for word in line.words)
    return line.end


def _first_spoken(line: Line) -> float:
    """Word time when we have one. A segment start still contains the pause."""

    spoken = [word.start for word in line.words if word.text.strip()]
    if spoken:
        return min(spoken)
    return line.start


def _start_at_first_word(setup: Line, lead_in: float) -> float:
    return _first_spoken(setup) - lead_in


def _quote_line(lines: list[Line], quote: str | None) -> Line | None:
    if not quote:
        return None
    from cliphunter.editorial.names import fold

    needle = fold(quote)
    if len(needle) < 4:
        return None
    for line in lines:
        if needle in fold(line.text):
            return line
    return None


def _topic_cut(lines: list[Line], after: float) -> float | None:
    from cliphunter.editorial.names import fold

    for line in lines:
        if line.start <= after + 0.05:
            continue
        text = fold(line.text)
        if text.endswith("?") or text.startswith("¿") or "?" in text[:24]:
            return line.start - 0.05
        if any(marker in text for marker in TOPIC_MARKERS):
            return line.start - 0.05
    return None


def _cap_trailing(lines: list[Line], reaction: Line, stop: float, reaction_cap: float, tail: float) -> float:
    from cliphunter.editorial.names import fold

    limit = _last_word_end(reaction) + reaction_cap
    for line in lines:
        if line.start <= reaction.end:
            continue
        if line.start > limit:
            break
        if any(token in fold(line.text) for token in LAUGHTER):
            limit = max(limit, _last_word_end(line) + tail)
    return min(stop, limit)


def _extend_backward(lines: list[Line], start: float, stop: float, minimum: float, lead_in: float) -> float:
    if stop - start >= minimum:
        return start
    earlier = [line for line in lines if _first_spoken(line) < start]
    cursor = start
    for line in reversed(earlier):
        if _topic_cut([line], line.start - 1) is not None:
            break
        cursor = _start_at_first_word(line, lead_in)
        if stop - cursor >= minimum:
            break
    return cursor


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
