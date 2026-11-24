"""Viral score, 0–100, rescaled over the components that actually exist."""

from __future__ import annotations

from typing import Any

CAPS = {
    "event_strength": 30.0,
    "reaction_quality": 15.0,
    "cultural_relevance": 15.0,
    "opening_hook": 15.0,
    "standalone_clarity": 10.0,
    "meme_potential": 10.0,
    "audience_engagement": 5.0,
}

CATEGORIES = {
    "drama": "drama",
    "chisme": "chisme",
    "humor": "humor",
    "humor dominicano": "humor",
    "morbo": "morbo",
    "meme": "meme",
    "meme potential": "meme",
    "celebrity": "celebrity",
    "celebridad": "celebrity",
    "social": "social",
    "dinamica social": "social",
    "dinámica social": "social",
}


def clamp_component(name: str, value: float | None) -> float | None:
    if value is None:
        return None
    cap = CAPS[name]
    return max(0.0, min(cap, float(value)))


def rescale(components: dict[str, float | None]) -> tuple[float, dict[str, float | None]]:
    """Turn partial components into a 0–100 score. Missing values are not zeros."""

    cleaned = {name: clamp_component(name, components.get(name)) for name in CAPS}
    present = {name: value for name, value in cleaned.items() if value is not None}
    cap = sum(CAPS[name] for name in present)
    if cap <= 0:
        return 0.0, cleaned
    total = round(100.0 * sum(present.values()) / cap, 1)
    return total, cleaned


def tier_for(score: float) -> str:
    if score >= 90:
        return "exceptional"
    if score >= 80:
        return "high"
    if score >= 65:
        return "secondary"
    return "rejected"


def normalize_category(value: str | None) -> str | None:
    if not value:
        return None
    key = " ".join(value.casefold().split())
    return CATEGORIES.get(key)


def storyline_is_known(note: str | None, known: list[str] | None) -> bool:
    """The note has to cite a storyline or relationship that is actually stored."""

    if not note or not known:
        return False
    folded = fold_note(note)
    for item in known:
        text = fold_note(item)
        if len(text) < 4:
            continue
        if text in folded or folded in text:
            return True
    return False


def fold_note(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def should_keep(
    total: float | None,
    *,
    storyline: bool,
    storyline_note: str | None,
    minimum: float = 65,
    floor: float = 50,
    known_storylines: list[str] | None = None,
) -> bool:
    if total is None:
        return False
    if total >= minimum:
        return True
    if total < floor or not storyline:
        return False
    return storyline_is_known(storyline_note, known_storylines)


def component_dict(raw: dict[str, Any] | None) -> dict[str, float | None]:
    source = raw or {}
    parsed: dict[str, float | None] = {}
    for name in CAPS:
        value = source.get(name)
        if value is None or value == "":
            parsed[name] = None
            continue
        parsed[name] = float(value)
    return parsed
