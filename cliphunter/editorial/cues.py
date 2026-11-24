"""Cheap transcript hints. They only nominate a window; they do not score it."""

from __future__ import annotations

from cliphunter.editorial.bounds import Line

LAUGHTER = ("jaja", "jeje", "jiji", "lol", "risa", "muerto", "jajaja")
SHIFT = (
    "traición",
    "traicion",
    "mentira",
    "indirecta",
    "te odio",
    "te quiero",
    "enamor",
    "celo",
    "pelea",
    "beso",
    "rompió",
    "rompio",
)


def find_cues(lines: list[Line], names: list[str]) -> list[str]:
    folded_names = [name.casefold() for name in names if name.strip()]
    found: list[str] = []
    for line in lines:
        text = line.text.casefold()
        if any(token in text for token in LAUGHTER):
            found.append("laughter")
        if "?" in line.text or "¿" in line.text:
            found.append("question")
        if any(token in text for token in SHIFT):
            found.append("shift")
        for name in folded_names:
            if name and name in text:
                found.append(f"name:{name}")
                break
    # Keep order, drop duplicates.
    unique: list[str] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique
