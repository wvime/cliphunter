"""Simulate a live stream with ffmpeg and cut a clip from the rolling buffer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from cliphunter.bus import STREAM_END, STREAM_START, Event, InMemoryEventBus
from cliphunter.cli import main
from cliphunter.config import channel_key
from cliphunter.ingest import IngestSession
from cliphunter.manager import IngestManager
from cliphunter.storage.repo import SqliteRepo
from tests.support import generate_ts, probe_duration, require_ffmpeg


def _ffmpeg_source(path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-c",
        "copy",
        "-flush_packets",
        "1",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def _write_config(tmp_path: Path) -> Path:
    buffer_dir = tmp_path / "buffer"
    clips_dir = tmp_path / "clips"
    database = tmp_path / "cliphunter.sqlite"
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f'buffer_dir: "{buffer_dir.as_posix()}"',
                f'clips_dir: "{clips_dir.as_posix()}"',
                f'database_path: "{database.as_posix()}"',
                "max_concurrent_streams: 2",
                "buffer_minutes: 10",
                "segment_seconds: 4",
                "poll_interval_seconds: 30",
                "channels:",
                "  - platform: youtube",
                "    handle: Tester",
                "    authorized: true",
                '    context: "canal de prueba"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config


async def _record(tmp_path: Path, source: Path) -> None:
    directory = tmp_path / "buffer" / channel_key_for_tester()
    session = IngestSession(
        channel_key=channel_key_for_tester(),
        directory=directory,
        segment_seconds=4,
        buffer_minutes=10,
        source_cmd_factory=lambda: _ffmpeg_source(source),
        session_meta={
            "session_id": None,
            "streamer": "Tester",
            "platform": "youtube",
            "stream_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "stream_title": "Test stream",
            "context": "canal de prueba",
        },
        platform="youtube",
        reconnect=False,
    )
    await asyncio.wait_for(session.run(), timeout=60)
    assert session.index is not None
    assert session.index.segments, [path.name for path in directory.iterdir()]


def channel_key_for_tester() -> str:
    from cliphunter.config import ChannelConfig

    return channel_key(ChannelConfig(platform="youtube", handle="Tester"))


def test_simulated_live_stream_can_be_clipped_from_the_cli(tmp_path: Path, capsys):
    require_ffmpeg()
    source = generate_ts(tmp_path / "live.ts", seconds=12)
    config = _write_config(tmp_path)
    asyncio.run(_record(tmp_path, source))
    output = tmp_path / "clips" / "manual.mp4"
    code = main(
        [
            "clip",
            "--config",
            str(config),
            "--canal",
            "Tester",
            "--ultimos",
            "6",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert output.exists()
    assert output.stat().st_size > 0
    assert "Clip guardado" in captured.out
    sidecar = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["streamer"] == "Tester"
    assert sidecar["platform"] == "youtube"
    assert sidecar["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert sidecar["stream_title"] == "Test stream"
    assert sidecar["clip_start"]
    assert sidecar["clip_end"]
    assert sidecar["requested_seconds"] == 6
    duration = probe_duration(output)
    assert 3 <= duration <= 14

    async def _rows() -> list[dict]:
        repo = SqliteRepo(tmp_path / "cliphunter.sqlite")
        await repo.init()
        try:
            return await repo.list_clips()
        finally:
            await repo.close()

    rows = asyncio.run(_rows())
    assert len(rows) == 1
    assert rows[0]["kind"] == "manual"
    assert rows[0]["status"] == "pending"
    stored = json.loads(rows[0]["attribution_json"])
    assert stored["streamer"] == "Tester"
    assert stored["platform"] == "youtube"
    assert stored["stream_title"] == "Test stream"


async def test_a_failed_stream_does_not_stop_the_other(tmp_path: Path):
    require_ffmpeg()
    source = generate_ts(tmp_path / "live.ts", seconds=8)
    good_dir = tmp_path / "good"
    bad_dir = tmp_path / "bad"
    good = IngestSession(
        channel_key="youtube_good",
        directory=good_dir,
        segment_seconds=4,
        buffer_minutes=10,
        source_cmd_factory=lambda: _ffmpeg_source(source),
        session_meta={"streamer": "Good", "platform": "youtube", "stream_url": "https://example.test/good", "stream_title": "Good"},
        platform="youtube",
        reconnect=False,
    )
    bad = IngestSession(
        channel_key="youtube_bad",
        directory=bad_dir,
        segment_seconds=4,
        buffer_minutes=10,
        source_cmd_factory=lambda: [sys.executable, "-c", "import sys; sys.exit(3)"],
        session_meta={"streamer": "Bad", "platform": "youtube"},
        platform="youtube",
        reconnect=False,
    )
    await asyncio.wait_for(asyncio.gather(good.run(), bad.run()), timeout=60)
    assert good.index is not None and good.index.segments
    assert list(good_dir.glob("*.ts"))


async def test_manager_records_a_session_and_keeps_going_when_one_source_fails(tmp_path: Path):
    require_ffmpeg()
    source = generate_ts(tmp_path / "live.ts", seconds=8)
    repo = SqliteRepo(tmp_path / "cliphunter.sqlite")
    await repo.init()
    bus = InMemoryEventBus()
    ids: dict[str, int] = {}
    confirmations: dict[str, str] = {}
    for key, handle in (("youtube_good", "good"), ("youtube_bad", "bad")):
        ids[key] = await repo.upsert_channel(
            channel_key=key,
            platform="youtube",
            handle=handle,
            url=None,
            authorized=True,
            context="",
            weights={},
            thresholds={},
        )
        confirmations[key] = "2026-11-18T00:00:00+00:00"

    def builder(payload: dict, _qualities: list[str]) -> list[str]:
        if payload["channel_key"] == "youtube_bad":
            return [sys.executable, "-c", "import sys; sys.exit(3)"]
        return _ffmpeg_source(source)

    manager = IngestManager(
        bus=bus,
        repo=repo,
        channel_ids=ids,
        confirmations=confirmations,
        buffer_dir=tmp_path / "buffer",
        segment_seconds=4,
        buffer_minutes=10,
        qualities=["480p"],
        max_concurrent=2,
        command_builder=builder,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(manager.run(stop))
    await manager.ready.wait()
    for key, handle, url in (
        ("youtube_good", "good", "https://www.youtube.com/watch?v=abcdefghijk"),
        ("youtube_bad", "bad", "https://example.test/bad"),
    ):
        await bus.publish(
            Event(
                STREAM_START,
                {
                    "channel_key": key,
                    "platform": "youtube",
                    "display_name": handle,
                    "stream_url": url,
                    "title": "Managed",
                    "video_id": key,
                    "context": "",
                },
            )
        )
    index_path = tmp_path / "buffer" / "youtube_good" / "index.json"
    found = False
    for _ in range(50):
        if index_path.exists():
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if payload["segments"]:
                found = True
                break
        await asyncio.sleep(0.2)
    assert found, [path.name for path in (tmp_path / "buffer").rglob("*")]
    await bus.publish(Event(STREAM_END, {"channel_key": "youtube_good"}))
    await bus.publish(Event(STREAM_END, {"channel_key": "youtube_bad"}))
    stop.set()
    await asyncio.wait_for(task, timeout=30)
    good_id = ids["youtube_good"]
    clips_dir = tmp_path / "buffer" / "youtube_good"
    assert list(clips_dir.glob("*.ts"))
    sessions = await repo.get_session(1)
    assert sessions is not None
    assert sessions["confirmed_authorized_at"] == "2026-11-18T00:00:00+00:00"
    assert sessions["status"] == "ended"
    assert sessions["channel_id"] == good_id
    await repo.close()
