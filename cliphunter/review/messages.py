"""Caption, inline keyboard, and the upload sidecar. No network here."""

from __future__ import annotations

import json
from typing import Any

BUTTONS = (
    ("Aprobar", "a"),
    ("Rechazar", "ask"),
    ("Inicio -5 s", "is"),
    ("Inicio +5 s", "ie"),
    ("Final -5 s", "fs"),
    ("Final +5 s", "fe"),
)

REJECT_REASONS = (
    ("aburrido", "aburrido"),
    ("mal cortado", "mal_cortado"),
    ("sin contexto", "sin_contexto"),
    ("repetido", "repetido"),
    ("otro", "otro"),
)

ADJUST_OPS = {
    "is": ("inicio", -5.0),
    "ie": ("inicio", 5.0),
    "fs": ("fin", -5.0),
    "fe": ("fin", 5.0),
}


def review_keyboard(clip_id: int) -> dict[str, Any]:
    cid = str(clip_id)
    rows = [
        [_button("Aprobar", f"a:{cid}"), _button("Rechazar", f"ask:{cid}")],
        [_button("Inicio -5 s", f"is:{cid}"), _button("Inicio +5 s", f"ie:{cid}")],
        [_button("Final -5 s", f"fs:{cid}"), _button("Final +5 s", f"fe:{cid}")],
    ]
    return {"inline_keyboard": rows}


def reason_keyboard(clip_id: int) -> dict[str, Any]:
    cid = str(clip_id)
    rows = [[_button(label, f"why:{cid}:{code}")] for label, code in REJECT_REASONS]
    return {"inline_keyboard": rows}


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def parse_callback(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "why":
        code, raw_id, reason = parts
        if reason not in {item[1] for item in REJECT_REASONS}:
            return None
        code = f"why:{reason}"
    elif len(parts) == 2:
        code, raw_id = parts
        if code not in {"a", "r", "ask", "is", "ie", "fs", "fe"}:
            return None
    else:
        return None
    try:
        clip_id = int(raw_id)
    except ValueError:
        return None
    return code, clip_id


def build_caption(item: dict[str, Any]) -> str:
    lines = [
        f"Canal: {item.get('channel') or 'desconocido'}",
        f"Inicio: {item.get('clip_start') or '-'}",
        f"Fin: {item.get('clip_end') or '-'}",
    ]
    if item.get("title"):
        lines.append(f"Título: {item['title']}")
    if item.get("category"):
        lines.append(f"Categoría: {item['category']}")
    participants = item.get("participants") or []
    if isinstance(participants, list) and participants:
        lines.append("Participantes: " + ", ".join(str(name) for name in participants))
    if item.get("why"):
        lines.append(f"Por qué: {item['why']}")
    if item.get("hook"):
        lines.append(f"Hook: {item['hook']}")
    viral = item.get("viral_score")
    if viral is not None:
        tier = item.get("tier") or ""
        lines.append(f"Viral: {viral}" + (f" ({tier})" if tier else ""))
    elif item.get("signal_score") is not None:
        lines.append(f"Score: {item.get('signal_score')}")
    if item.get("editorial_mode") == "signal-only":
        lines.append("Modo: solo señales")
    breakdown = item.get("breakdown")
    if breakdown:
        encoded = json.dumps(breakdown, ensure_ascii=False)
        lines.append(f"Breakdown: {encoded}")
    text = "\n".join(lines)
    return text[:1024]


def ready_sidecar_text(attribution: dict[str, Any]) -> str:
    """Text that can be pasted into a post. Attribution stays out of this file.

    ``credit`` and other record-keeping fields live only in the JSON sidecar
    and in ``attribution_json``.
    """

    title = attribution.get("title") or ""
    hook = attribution.get("hook") or ""
    tags = attribution.get("hashtags") or ""
    if isinstance(tags, list):
        tags = " ".join(str(tag) for tag in tags)
    lines = [f"title: {title}", f"hook: {hook}", f"hashtags: {tags}"]
    editorial = attribution.get("editorial_mode") == "editorial" or attribution.get("viral_score") is not None
    if editorial:
        if attribution.get("viral_score") is not None:
            lines.append(f"score: {attribution.get('viral_score')}")
        if attribution.get("category"):
            lines.append(f"category: {attribution.get('category')}")
        participants = attribution.get("participants") or []
        if isinstance(participants, list):
            participants = ", ".join(str(name) for name in participants)
        if participants:
            lines.append(f"participants: {participants}")
        if attribution.get("why"):
            lines.append(f"why: {attribution.get('why')}")
        if attribution.get("clip_start"):
            lines.append(f"start: {attribution.get('clip_start')}")
        if attribution.get("clip_end"):
            lines.append(f"end: {attribution.get('clip_end')}")
    return "\n".join(lines) + "\n"
