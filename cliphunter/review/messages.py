"""Caption, inline keyboard, and the upload sidecar. No network here."""

from __future__ import annotations

import json
from typing import Any

BUTTONS = (
    ("Aprobar", "a"),
    ("Rechazar", "r"),
    ("Ajustar -5s inicio", "is"),
    ("Ajustar +5s inicio", "ie"),
    ("Ajustar -5s fin", "fs"),
    ("Ajustar +5s fin", "fe"),
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
        [_button("Aprobar", f"a:{cid}"), _button("Rechazar", f"r:{cid}")],
        [_button("Ajustar -5s inicio", f"is:{cid}"), _button("Ajustar +5s inicio", f"ie:{cid}")],
        [_button("Ajustar -5s fin", f"fs:{cid}"), _button("Ajustar +5s fin", f"fe:{cid}")],
    ]
    return {"inline_keyboard": rows}


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def parse_callback(data: str) -> tuple[str, int] | None:
    if ":" not in data:
        return None
    code, raw_id = data.split(":", 1)
    if code not in {"a", "r", "is", "ie", "fs", "fe"}:
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
    score = item.get("signal_score")
    if score is not None:
        lines.append(f"Score: {score}")
    breakdown = item.get("breakdown")
    if breakdown:
        encoded = json.dumps(breakdown, ensure_ascii=False)
        lines.append(f"Breakdown: {encoded}")
    text = "\n".join(lines)
    return text[:1024]


def ready_sidecar_text(attribution: dict[str, Any]) -> str:
    title = attribution.get("title") or ""
    hook = attribution.get("hook") or ""
    tags = attribution.get("hashtags") or ""
    if isinstance(tags, list):
        tags = " ".join(str(tag) for tag in tags)
    credit = str(attribution.get("credit") or "").strip()
    lines = [f"title: {title}", f"hook: {hook}", f"hashtags: {tags}", ""]
    if credit:
        lines.append(credit)
    return "\n".join(lines).rstrip() + "\n"
