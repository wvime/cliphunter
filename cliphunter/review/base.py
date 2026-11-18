"""Notifier interface shared by Telegram and the localhost fallback."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class Decision(BaseModel):
    action: Literal["approve", "reject", "adjust"]
    adjust_seconds: float = 0
    note: str | None = None


class Notifier(Protocol):
    async def send(self, item: dict[str, Any]) -> None: ...

    async def run(self, stop: asyncio.Event) -> None: ...


class ReviewBackend(Protocol):
    async def request_review(self, clip_id: int, attribution: dict) -> Decision: ...
