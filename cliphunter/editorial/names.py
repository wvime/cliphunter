"""A name may appear only if that clip's transcript actually says it."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_TOKEN = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)
NEUTRAL = "un participante"
# Ordinary Spanish words that must never count as a person, even if the spelling is close.
STOPWORDS = frozenset(
    """
    a al algo alguna algunas alguno algunos ante aquel aquella aquellas aquellos
    como con contra cual cuales cuando de del desde donde dos el ella ellas ellos
    en entre era es esa esas ese esos esta estas este estos fue gran ha han hasta
    hay la las le les lo los mas más me mi mis muy ni no nos o os para pero por
    porque que se sea ser si sí sin sobre su sus te ti tu tus un una uno y ya
    """.split()
)


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.casefold()


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, fold(left), fold(right)).ratio()


def name_said(name: str, transcript: str, *, threshold: float = 0.92) -> bool:
    """Side lookup only. Never used to rewrite the transcript.

    Fuzzy matches ignore stopwords, so ``gran`` cannot become ``Germán``.
    """

    if not name or not transcript:
        return False
    needle = fold(name).strip()
    if needle in STOPWORDS or len(needle) < 3:
        return False
    hay = fold(transcript)
    if needle in hay:
        return True
    if len(needle) < 4:
        return False
    tokens = [token for token in _TOKEN.findall(hay) if token not in STOPWORDS and len(token) >= 4]
    parts = [part for part in needle.split() if part not in STOPWORDS and len(part) >= 4]
    if not parts:
        return False
    return all(
        any(_ratio(token, part) >= threshold and abs(len(token) - len(part)) <= 2 for token in tokens)
        for part in parts
    )


def supported_names(transcript: str, people: list[dict]) -> list[str]:
    found: list[str] = []
    for person in people:
        if any(name_said(token, transcript) for token in protected_tokens([person])):
            canonical = str(person.get("name") or "").strip()
            if canonical and canonical not in found:
                found.append(canonical)
    return found


def protected_tokens(people: list[dict]) -> list[str]:
    """First names, last names, and apodos. Stopwords are not names."""

    found: list[str] = []
    for person in people:
        labels = [str(person.get("name") or "")]
        labels.extend(str(nick) for nick in (person.get("nicknames") or []))
        for label in labels:
            for part in fold(label).split():
                if len(part) >= 4 and part not in STOPWORDS and part not in found:
                    found.append(part)
    return found


def ground_verdict(verdict, transcript: str, people: list[dict]):
    """Drop every name the transcript does not support. Titles included."""

    if verdict.mode != "editorial":
        return verdict
    allowed = supported_names(transcript, people)
    allowed_fold = {fold(name) for name in allowed}
    participants = []
    for name in verdict.participants:
        if fold(name) in allowed_fold or name_said(name, transcript):
            canonical = next((item for item in allowed if fold(item) == fold(name)), name)
            if canonical not in participants:
                participants.append(canonical)
    blocked = [name for name in verdict.participants if fold(name) not in {fold(item) for item in participants}]
    blocked.extend(token for token in protected_tokens(people) if not name_said(token, transcript))
    title = _scrub(verdict.title, blocked)
    hook = _scrub(verdict.hook, blocked)
    why = _scrub(verdict.why, blocked)
    sujeto = verdict.sujeto
    if sujeto and not (fold(sujeto) in allowed_fold or name_said(sujeto, transcript)):
        sujeto = None
        if title and verdict.sujeto and fold(verdict.sujeto) in fold(title):
            title = _scrub(title, [verdict.sujeto])
    return verdict.model_copy(
        update={
            "participants": participants,
            "title": title,
            "hook": hook,
            "why": why,
            "sujeto": sujeto,
        }
    )


def _scrub(text: str | None, names: list[str]) -> str | None:
    if not text:
        return text
    cleaned = text
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        cleaned = pattern.sub(NEUTRAL, cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or None
