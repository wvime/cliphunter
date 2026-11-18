import json

from cliphunter.bus import Event, InMemoryEventBus
from cliphunter.jsonlog import log_event, set_level


async def test_subscribe_filters_event_types():
    bus = InMemoryEventBus()
    queue = await bus.subscribe("stream.start")
    await bus.publish(Event("stream.end", {"n": 1}))
    assert queue.empty()
    await bus.publish(Event("stream.start", {"n": 2}))
    event = queue.get_nowait()
    assert event.type == "stream.start"
    assert event.payload == {"n": 2}
    await bus.close()


async def test_full_queue_drops_oldest():
    bus = InMemoryEventBus(maxsize=1)
    queue = await bus.subscribe("stream.start")
    await bus.publish(Event("stream.start", {"n": 1}))
    await bus.publish(Event("stream.start", {"n": 2}))
    event = queue.get_nowait()
    assert event.payload["n"] == 2
    assert bus.dropped == 1


def test_logs_redact_secrets(capsys):
    set_level("INFO")
    log_event("INFO", "demo", api_key="super-secret", note="https://example.test/a?key=abc&x=1")
    line = capsys.readouterr().out
    payload = json.loads(line)
    assert payload["api_key"] == "***"
    assert "abc" not in line
    assert "super-secret" not in line
