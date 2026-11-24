from cliphunter.bus import InMemoryEventBus
from cliphunter.config import ChannelConfig
from cliphunter.platforms.base import LiveStatus
from cliphunter.platforms.kick import KickPlatform
from cliphunter.watcher import Watcher


class _Scripted:
    name = "youtube"

    def __init__(self, statuses: list[LiveStatus]) -> None:
        self.statuses = list(statuses)

    async def check_live(self, _channel: ChannelConfig) -> LiveStatus:
        if self.statuses:
            return self.statuses.pop(0)
        return LiveStatus(is_live=False, reason="offline")


class _Boom:
    name = "youtube"

    async def check_live(self, _channel: ChannelConfig) -> LiveStatus:
        raise RuntimeError("network")


def _channel() -> ChannelConfig:
    return ChannelConfig(platform="youtube", handle="Tester", authorized=True, context="es")


async def test_watcher_emits_start_then_end_and_ignores_poll_errors():
    bus = InMemoryEventBus()
    queue = await bus.subscribe("stream.start", "stream.end")
    platform = _Scripted(
        [
            LiveStatus(
                is_live=True,
                stream_url="https://www.youtube.com/watch?v=abcdefghijk",
                title="Live",
                video_id="abcdefghijk",
            ),
            LiveStatus(is_live=False, reason="offline"),
        ]
    )
    watcher = Watcher(
        channels=[_channel()],
        platforms={"youtube": platform},
        bus=bus,
        interval=0.01,
    )
    await watcher.poll_once()
    await watcher.poll_once()
    start = queue.get_nowait()
    end = queue.get_nowait()
    assert start.type == "stream.start"
    assert start.payload["video_id"] == "abcdefghijk"
    assert start.payload["context"] == "es"
    assert end.type == "stream.end"
    assert queue.empty()

    watcher.platforms["youtube"] = _Boom()
    watcher._live[start.payload["channel_key"]] = "abcdefghijk"
    await watcher.poll_once()
    assert queue.empty()
    assert start.payload["channel_key"] in watcher._live


async def test_kick_stub_never_goes_live():
    bus = InMemoryEventBus()
    queue = await bus.subscribe("stream.start", "stream.end")
    channel = ChannelConfig(platform="kick", handle="someone", authorized=True)
    watcher = Watcher(
        channels=[channel],
        platforms={"kick": KickPlatform()},
        bus=bus,
        interval=0.01,
    )
    await watcher.poll_once()
    await watcher.poll_once()
    assert queue.empty()
    assert watcher._live == {}
