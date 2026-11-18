"""Human review interface. Telegram Approve / Reject / Adjust ±5s comes later."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel


class Decision(BaseModel):
    action: Literal["approve", "reject", "adjust"]
    adjust_seconds: float = 0
    note: str | None = None


class ReviewBackend(Protocol):
    async def request_review(self, clip_id: int, attribution: dict) -> Decision: ...
