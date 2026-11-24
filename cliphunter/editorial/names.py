"""A name may appear only if that clip's transcript actually says it."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_TOKEN = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)
NEUTRAL = "un participante"


def fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.casefold()


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, fold(left), fold(right)).ratio()


def name_said(name: str, transcript: str, *, threshold: float = 0.8) -> bool:
    """True when ``name`` or a close whisper spelling is in ``transcript``."""

    if not name or not transcript:
        return False
    needle = fold(name).strip()
    hay = fold(transcript)
    if len(needle) >= 3 and needle in hay:
        return True
    tokens = _TOKEN.findall(hay)
    parts = [part for part in needle.split() if len(part) >= 3]
    if not parts:
        return False
    if len(parts) == 1:
        return any(_ratio(token, parts[0]) >= threshold for token in tokens if len(token) >= 4)
    return all(any(_ratio(token, part) >= threshold for token in tokens if len(token) >= 3) for part in parts)


def supported_names(transcript: str, people: list[dict]) -> list[str]:
    found: list[str] = []
    for person in people:
        labels = [str(person.get("name") or "")]
        labels.extend(str(nick) for nick in (person.get("nicknames") or []))
        if any(name_said(label, transcript) for label in labels if label.strip()):
            canonical = str(person.get("name") or labels[0]).strip()
            if canonical and canonical not in found:
                found.append(canonical)
    return found


def correct_tokens(text: str, names: list[str], *, threshold: float = 0.8) -> str:
    """Replace a whisper token with a participant name when the spelling is close."""

    if not text or not names:
        return text
    canon = [name for name in names if len(fold(name)) >= 4]

    def _swap(match: re.Match[str]) -> str:
        token = match.group(0)
        if len(token) < 4:
            return token
        best = ""
        best_score = 0.0
        for name in canon:
            score = _ratio(token, name.split()[0] if " " not in fold(token) else name)
            if score > best_score:
                best, best_score = name.split()[0] if " " in name and " " not in token else name, score
        if best and best_score >= threshold and fold(token) != fold(best):
            return best
        return token

    return _TOKEN.sub(_swap, text)


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
    blob = " ".join(part for part in (verdict.title, verdict.hook, verdict.why, verdict.sujeto) if part)
    for person in people:
        official = str(person.get("name") or "")
        if not official or fold(official) in allowed_fold:
            continue
        labels = [official, *[str(nick) for nick in (person.get("nicknames") or [])]]
        if any(label and fold(label) in fold(blob) for label in labels):
            blocked.append(official)
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
