"""YouTube live chat without the Data API.

Polling uses the public Innertube ``live_chat/get_live_chat`` endpoint
(the same idea as pytchat). That call does not spend YouTube Data API
quota. ``liveChatMessages.list`` is intentionally not used: each poll
costs quota and would not fit a 2 s loop.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from cliphunter.signals.base import ChatMessage

_CONTINUATION = re.compile(r'"continuation"\s*:\s*"([^"]+)"')
_CLIENT = {
    "context": {
        "client": {
            "clientName": "WEB",
            "clientVersion": "2.20241118.01.00",
            "hl": "es",
            "gl": "ES",
        }
    }
}


def messages_from_payload(payload: dict[str, Any], *, now: float) -> tuple[list[ChatMessage], str | None]:
    continuation = _find_continuation(payload)
    found: list[ChatMessage] = []
    for renderer in _walk_renderers(payload):
        text = _render_text(renderer.get("message") or {})
        if not text:
            continue
        usec = renderer.get("timestampUsec")
        if isinstance(usec, str) and usec.isdigit():
            stamp = int(usec) / 1_000_000
        else:
            stamp = now
        found.append(ChatMessage(text=text, t=stamp))
    return found, continuation


def _find_continuation(node: Any) -> str | None:
    if isinstance(node, dict):
        invalid = node.get("invalidationContinuationData") or node.get("timedContinuationData")
        if isinstance(invalid, dict) and isinstance(invalid.get("continuation"), str):
            return invalid["continuation"]
        for value in node.values():
            found = _find_continuation(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_continuation(value)
            if found:
                return found
    return None


def _walk_renderers(node: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        renderer = node.get("liveChatTextMessageRenderer")
        if isinstance(renderer, dict):
            found.append(renderer)
        for value in node.values():
            found.extend(_walk_renderers(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_renderers(value))
    return found


def _render_text(message: dict) -> str:
    runs = message.get("runs") or []
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if isinstance(run.get("text"), str):
            parts.append(run["text"])
        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            shortcuts = emoji.get("shortcuts") or []
            if shortcuts:
                parts.append(str(shortcuts[0]))
            label = ((emoji.get("image") or {}).get("accessibility") or {}).get("accessibilityData") or {}
            if isinstance(label, dict) and label.get("label"):
                parts.append(str(label["label"]))
    return "".join(parts).strip()


class YouTubeChatClient:
    """Best-effort Innertube poller. Failures stay inside the caller."""

    def __init__(self, video_id: str, client: httpx.AsyncClient | None = None) -> None:
        self.video_id = video_id
        self._client = client
        self._owns = client is None
        self.continuation: str | None = None

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()

    async def poll(self, now: float) -> list[ChatMessage]:
        client = await self._http()
        if self.continuation is None:
            page = await client.get(
                "https://www.youtube.com/live_chat",
                params={"v": self.video_id, "is_popout": "1"},
                timeout=10,
            )
            match = _CONTINUATION.search(page.text)
            if not match:
                return []
            self.continuation = match.group(1)
        body = dict(_CLIENT)
        body["continuation"] = self.continuation
        response = await client.post(
            "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
            params={"prettyPrint": "false"},
            json=body,
            timeout=10,
        )
        payload = response.json()
        messages, continuation = messages_from_payload(payload, now=now)
        if continuation:
            self.continuation = continuation
        return messages

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "es-ES,en;q=0.8"},
                follow_redirects=True,
            )
        return self._client
