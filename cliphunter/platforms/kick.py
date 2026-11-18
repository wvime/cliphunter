"""Kick placeholder. The Platform interface is real; live detection is not."""

from __future__ import annotations

from cliphunter.config import ChannelConfig
from cliphunter.platforms.base import LiveStatus


class KickPlatform:
    """Phase 1 stub.

    Kick has no stable official API we can depend on here. Returning offline
    with ``reason='kick_not_implemented'`` keeps the watcher from recording
    while later phases can replace this class without touching callers.
    """

    name = "kick"
    implemented = False

    async def check_live(self, channel: ChannelConfig) -> LiveStatus:
        del channel
        return LiveStatus(is_live=False, reason="kick_not_implemented", definitive=True)
