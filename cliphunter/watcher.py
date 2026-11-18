"""Poll platforms and emit stream.start / stream.end on transitions.

A failed poll keeps the previous state so a network blip does not stop a
recording. Only a definitive offline result ends the stream.
"""

from __future__ import annotations

import asyncio

from cliphunter.bus import STREAM_END, STREAM_START, Event, EventBus
from cliphunter.config import ChannelConfig, channel_key
from cliphunter.jsonlog import log_event
from cliphunter.platforms.base import LiveStatus, Platform

_SPECIAL = {
    "kick_not_implemented": "Kick no está implementado en la fase 1; este canal no se graba.",
    "missing_credentials": (
        "Faltan TWITCH_CLIENT_ID y TWITCH_CLIENT_SECRET; este canal de Twitch no se vigila."
    ),
}


class Watcher:
    def __init__(
        self,
        *,
        channels: list[ChannelConfig],
        platforms: dict[str, Platform],
        bus: EventBus,
        interval: float,
    ) -> None:
        self.channels = list(channels)
        self.platforms = platforms
        self.bus = bus
        self.interval = interval
        self._live: dict[str, str] = {}
        self._seen: set[str] = set()
        self._noted: set[str] = set()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                continue

    async def poll_once(self) -> None:
        for channel in self.channels:
            key = channel_key(channel)
            platform = self.platforms.get(channel.platform)
            if platform is None:
                log_event(
                    "ERROR",
                    "watcher.unknown_platform",
                    channel=key,
                    platform=channel.platform,
                )
                continue
            try:
                status = await platform.check_live(channel)
            except Exception as exc:
                log_event(
                    "ERROR",
                    "watcher.poll_failed",
                    channel=key,
                    error=str(exc),
                    kept_state=key in self._live,
                )
                continue
            for event in self._transition(channel, status):
                await self.bus.publish(event)

    def _transition(self, channel: ChannelConfig, status: LiveStatus) -> list[Event]:
        key = channel_key(channel)
        if status.reason in _SPECIAL and key not in self._noted:
            self._noted.add(key)
            log_event("WARNING", "watcher.unavailable", channel=key, reason=status.reason, message=_SPECIAL[status.reason])
        if not status.definitive:
            return []
        current = self._live.get(key)
        events: list[Event] = []
        if status.is_live:
            video = status.video_id or status.stream_url or "live"
            if current is None:
                self._live[key] = video
                events.append(Event(STREAM_START, self._start_payload(channel, status, video)))
                log_event(
                    "INFO",
                    "stream.live",
                    channel=key,
                    video_id=status.video_id,
                    title=status.title,
                    url=status.stream_url,
                )
            elif current != video:
                events.append(Event(STREAM_END, self._end_payload(channel, current)))
                self._live[key] = video
                events.append(Event(STREAM_START, self._start_payload(channel, status, video)))
                log_event("INFO", "stream.replaced", channel=key, video_id=status.video_id)
        else:
            if current is not None:
                events.append(Event(STREAM_END, self._end_payload(channel, current)))
                self._live.pop(key, None)
                log_event("INFO", "stream.offline", channel=key, reason=status.reason)
            elif key not in self._seen and status.reason not in _SPECIAL:
                log_event("INFO", "stream.offline", channel=key, reason=status.reason)
        self._seen.add(key)
        return events

    def _start_payload(self, channel: ChannelConfig, status: LiveStatus, video: str) -> dict:
        return {
            "channel_key": channel_key(channel),
            "platform": channel.platform,
            "handle": channel.handle,
            "channel_url": channel.url,
            "display_name": channel.display_name,
            "stream_url": status.stream_url,
            "title": status.title,
            "video_id": status.video_id or video,
            "context": channel.context,
            "context_fields": dict(channel.context_fields),
            "weights": channel.weights,
            "thresholds": channel.thresholds,
            "authorization": channel.authorization_payload(),
            "emotes_hype": list(channel.emotes_hype),
        }

    def _end_payload(self, channel: ChannelConfig, video: str) -> dict:
        return {
            "channel_key": channel_key(channel),
            "platform": channel.platform,
            "display_name": channel.display_name,
            "video_id": video,
        }
