"""Claude editorial judge. Without a key this stays signal-only and invents nothing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from cliphunter.editorial import PROMPT_VERSION
from cliphunter.editorial.names import ground_verdict
from cliphunter.editorial.score import component_dict, normalize_category, rescale, should_keep, tier_for

PROMPT_PATH = Path(__file__).with_name("prompts") / "director_v2.md"


class EditorialVerdict(BaseModel):
    mode: str = "signal-only"
    total: float | None = None
    tier: str | None = None
    category: str | None = None
    participants: list[str] = Field(default_factory=list)
    setup_start: float | None = None
    payoff: float | None = None
    payoff_quote: str | None = None
    end: float | None = None
    hook: str | None = None
    title: str | None = None
    sujeto: str | None = None
    why: str | None = None
    storyline: bool = False
    storyline_note: str | None = None
    components: dict[str, float | None] = Field(default_factory=dict)
    prompt_version: str = PROMPT_VERSION
    keep: bool = False

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump()


def signal_only_verdict(signal_score: float | None = None) -> EditorialVerdict:
    """The old scorer. Editorial fields stay empty on purpose."""

    del signal_score
    return EditorialVerdict(
        mode="signal-only",
        total=None,
        tier=None,
        why=None,
        components={},
        keep=False,
        storyline_note=None,
        title=None,
        hook=None,
        sujeto=None,
        category=None,
        participants=[],
    )


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if "```" in raw:
            raw = raw.rsplit("```", 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("la respuesta no trae un objeto JSON")
    payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("el JSON editorial no es un objeto")
    return payload


def verdict_from_payload(payload: dict[str, Any], *, minimum: float = 65) -> EditorialVerdict:
    components_in = payload.get("components") if isinstance(payload.get("components"), dict) else {}
    total, components = rescale(component_dict(components_in))
    storyline = bool(payload.get("storyline"))
    note = payload.get("storyline_note")
    note_text = str(note).strip() if isinstance(note, str) and note.strip() else None
    if storyline and not note_text:
        storyline = False
    category = normalize_category(payload.get("category") if isinstance(payload.get("category"), str) else None)
    participants = [str(item).strip() for item in (payload.get("participants") or []) if str(item).strip()]
    verdict = EditorialVerdict(
        mode="editorial",
        total=total,
        tier=tier_for(total),
        category=category,
        participants=participants,
        setup_start=_float(payload.get("setup_start")),
        payoff=_float(payload.get("payoff")),
        payoff_quote=_text(payload.get("payoff_quote")),
        end=_float(payload.get("end")),
        hook=_text(payload.get("hook")),
        title=_text(payload.get("title")),
        sujeto=_text(payload.get("sujeto")),
        why=_text(payload.get("why")),
        storyline=storyline,
        storyline_note=note_text,
        components=components,
        keep=total >= minimum,
    )
    return verdict


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _packet_transcript(packet: dict[str, Any]) -> str:
    lines = packet.get("transcript") or []
    parts = []
    for line in lines:
        if isinstance(line, dict):
            parts.append(str(line.get("text") or ""))
        else:
            parts.append(str(line))
    return " ".join(parts)


def _average(first: EditorialVerdict, second: EditorialVerdict, packet: dict[str, Any], *, minimum: float, floor: float) -> EditorialVerdict:
    from cliphunter.editorial.score import CAPS

    merged: dict[str, float | None] = {}
    for name in CAPS:
        left = first.components.get(name)
        right = second.components.get(name)
        if left is None and right is None:
            merged[name] = None
        elif left is None:
            merged[name] = right
        elif right is None:
            merged[name] = left
        else:
            merged[name] = (left + right) / 2
    total, components = rescale(merged)
    known = [str(item) for item in (packet.get("storylines") or [])]
    base = first if (first.total or 0) >= (second.total or 0) else second
    return base.model_copy(
        update={
            "total": total,
            "components": components,
            "tier": tier_for(total),
            "keep": should_keep(
                total,
                storyline=base.storyline,
                storyline_note=base.storyline_note,
                minimum=minimum,
                floor=floor,
                known_storylines=known,
            ),
        }
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


class ClaudeJudge:
    """Messages API over httpx. Tests pass a fake ``complete`` callable."""

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = "claude-haiku-4-5",
        complete: Any = None,
        minimum: float = 65,
        floor: float = 50,
        resample: bool = True,
        resample_low: float = 58,
        resample_high: float = 72,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.minimum = minimum
        self.floor = floor
        self.resample = resample
        self.resample_low = resample_low
        self.resample_high = resample_high
        self._complete = complete

    async def judge(self, packet: dict[str, Any]) -> EditorialVerdict:
        if not self.api_key and self._complete is None:
            return signal_only_verdict(packet.get("signal_score"))
        try:
            first = self._finish(extract_json(await self._ask(packet)), packet)
            if (
                not self.resample
                or first.mode != "editorial"
                or first.total is None
                or not (self.resample_low <= first.total <= self.resample_high)
            ):
                return first
            second = self._finish(extract_json(await self._ask(packet)), packet)
            if second.mode != "editorial":
                return first
            return _average(first, second, packet, minimum=self.minimum, floor=self.floor)
        except Exception:
            return signal_only_verdict(packet.get("signal_score"))

    def _finish(self, payload: dict[str, Any], packet: dict[str, Any]) -> EditorialVerdict:
        verdict = verdict_from_payload(payload, minimum=self.minimum)
        transcript = _packet_transcript(packet)
        people = list(packet.get("people") or [])
        verdict = ground_verdict(verdict, transcript, people)
        known = [str(item) for item in (packet.get("storylines") or []) if str(item).strip()]
        return verdict.model_copy(
            update={
                "keep": should_keep(
                    verdict.total,
                    storyline=verdict.storyline,
                    storyline_note=verdict.storyline_note,
                    minimum=self.minimum,
                    floor=self.floor,
                    known_storylines=known,
                )
            }
        )

    async def _ask(self, packet: dict[str, Any]) -> str:
        if self._complete is not None:
            return await self._complete(packet)
        if not self.api_key:
            raise RuntimeError("sin ANTHROPIC_API_KEY")
        body = {
            "model": self.model,
            "max_tokens": 900,
            "temperature": 0,
            "system": load_prompt(),
            "messages": [{"role": "user", "content": json.dumps(packet, ensure_ascii=False)}],
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
        response.raise_for_status()
        payload = response.json()
        parts = payload.get("content") or []
        texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        return "\n".join(texts)
