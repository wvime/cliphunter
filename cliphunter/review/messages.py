"""Caption, inline keyboard, and the upload sidecar. No network here."""

from __future__ import annotations

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
    """Title first. One compact second line. No transcript, hook, or why."""

    title = str(item.get("title") or "").strip() or str(item.get("channel") or "Clip")
    bits: list[str] = []
    score = item.get("viral_score")
    if score is None:
        score = item.get("signal_score")
    if score is not None:
        bits.append(str(score))
    if item.get("category"):
        bits.append(str(item["category"]))
    participants = item.get("participants") or []
    if isinstance(participants, list) and participants:
        bits.append(", ".join(str(name) for name in participants))
    second = " · ".join(bits)
    label = str(item.get("adjust_label") or "").strip()
    if label:
        second = f"{second} {label}".strip()
    lines = [title]
    if second:
        lines.append(second)
    return "\n".join(lines)[:1024]


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
