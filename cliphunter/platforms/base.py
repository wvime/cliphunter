"""Platform interface shared by YouTube, Twitch and Kick."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cliphunter.config import ChannelConfig


class PlatformError(Exception):
    """A check failed in a way that should not change live/offline state."""


@dataclass(frozen=True)
class LiveStatus:
    is_live: bool
    stream_url: str | None = None
    title: str | None = None
    started_at: str | None = None
    video_id: str | None = None
    reason: str | None = None
    # False means "ignore this sample" (network error, consent wall, ...).
    definitive: bool = True


class Platform(Protocol):
    name: str

    async def check_live(self, channel: ChannelConfig) -> LiveStatus: ...
