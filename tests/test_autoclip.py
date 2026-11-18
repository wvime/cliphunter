"""Automatic cuts from a simulated buffer, plus learning from decisions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cliphunter.bus import InMemoryEventBus
from cliphunter.calibrate import calibrate_command
from cliphunter.cli import main
from cliphunter.config import AppConfig, ChannelConfig
from cliphunter.index import Segment, SegmentIndex
from cliphunter.signals.base import ChatMessage
from cliphunter.signals.engine import AutoClipEngine, LiveView
from cliphunter.storage.repo import SqliteRepo
from tests.support import generate_ts, probe_duration, require_ffmpeg


def _config(tmp_path: Path, *, threshold: float = 2.5) -> AppConfig:
    return AppConfig(
        buffer_dir=tmp_path / "buffer",
        clips_dir=tmp_path / "clips",
        database_path=tmp_path / "cliphunter.sqlite",
        transcript_on_trigger=False,
        channels=[
            ChannelConfig(
                platform="youtube",
                handle="Tester",
                authorized=True,
                weights={"chat": 0.35, "emotes": 0.25, "audio": 0.30, "scene": 0.05, "viewers": 0.05},
                thresholds={"signal_score": threshold, "cooldown_s": 45},
                emotes_hype=["😂", "🤣", "💀", "🔥", "JAJAJA", "jajaja"],
            )
        ],
    )


def _view(config: AppConfig, index: Path, channel_id: int) -> LiveView:
    channel = config.channels[0]
    return LiveView(
        channel_key="youtube_Tester",
        channel_db_id=channel_id,
        index_path=index,
        video_id=None,
        config_id=channel.id,
        emotes=list(channel.emotes_hype),
        weights=dict(channel.weights),
        thresholds=dict(channel.thresholds),
        credit=None,
        authorization=None,
    )


async def _drive(engine: AutoClipEngine, view: LiveView, base: float) -> list:
    scores = []
    for step in range(6):
        tick = await engine.tick(view, base + step * 2)
        assert tick is not None
        scores.append(tick.score)
    burst = [ChatMessage("JAJAJA 😂", base + 12 - 0.05 * n) for n in range(12)]
    engine.note_chat(view.channel_key, burst)
    rising = await engine.tick(view, base + 12)
    assert rising is not None
    scores.append(rising.score)
    # The burst is still inside the 10 s window two seconds later. Wait it out.
    falling = await engine.tick(view, base + 24)
    assert falling is not None
    scores.append(falling.score)
    return scores


def test_chat_burst_fires_without_media_and_a_broken_signal_does_not_stop_it(tmp_path: Path):
    config = _config(tmp_path)

    class Flaky(AutoClipEngine):
        def _signals(self, view):
            signals = super()._signals(view)

            class Boom:
                name = "scene_change"

                def compute(self, window):
                    raise RuntimeError("scene rota")

            return [Boom() if signal.name == "scene_change" else signal for signal in signals]

    async def _run() -> None:
        repo = SqliteRepo(config.resolved_database_path())
        await repo.init()
        try:
            channel_id = await repo.upsert_channel(
                channel_key="youtube_Tester",
                platform="youtube",
                handle="Tester",
                url=None,
                authorized=True,
                context="",
                weights={},
                thresholds={"signal_score": 2.5},
            )
            engine = Flaky(config=config, repo=repo, bus=InMemoryEventBus(), channels=config.channels)
            view = _view(config, tmp_path / "missing" / "index.json", channel_id)
            base = datetime.now(timezone.utc).timestamp()
            scores = await _drive(engine, view, base)
            assert max(scores) >= 2.5
            state = engine._states[view.channel_key]
            assert state.cooldown_until >= base + 24
            assert "scene_change" in state.degraded_logged
        finally:
            await repo.close()

    asyncio.run(_run())


def test_simulated_buffer_is_cut_automatically_between_15_and_45(tmp_path: Path):
    require_ffmpeg()
    # A flat sine wave keeps audio z near 0, so a chat-only spike lands under 2.5.
    # 1.5 still requires the burst; a real laugh would push audio as well.
    config = _config(tmp_path, threshold=1.5)
    media = generate_ts(tmp_path / "seg.ts", seconds=4)
    directory = tmp_path / "buffer" / "youtube_Tester"
    directory.mkdir(parents=True)
    target = directory / "seg.ts"
    target.write_bytes(media.read_bytes())
    origin = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=40)
    segments = []
    for index in range(16):
        start = origin + timedelta(seconds=index * 4)
        segments.append(
            Segment(
                file="seg.ts",
                duration=4,
                wall_start=start.isoformat(),
                wall_end=(start + timedelta(seconds=4)).isoformat(),
                streamer="Tester",
                platform="youtube",
                stream_url="https://www.youtube.com/watch?v=abcdefghijk",
                stream_title="Test stream",
            )
        )
    index = SegmentIndex(
        channel_key="youtube_Tester",
        platform="youtube",
        directory=directory,
        session={"streamer": "Tester", "platform": "youtube", "stream_url": "https://www.youtube.com/watch?v=abcdefghijk"},
        segments=segments,
    )
    index.save()
    woken: list[str] = []

    async def _run() -> tuple[list, float]:
        repo = SqliteRepo(config.resolved_database_path())
        await repo.init()
        bus = InMemoryEventBus()
        queue = await bus.subscribe("moment.candidate")
        try:
            channel_id = await repo.upsert_channel(
                channel_key="youtube_Tester",
                platform="youtube",
                handle="Tester",
                url="https://www.youtube.com/@Tester",
                authorized=True,
                context="",
                weights={"chat": 0.35},
                thresholds={"signal_score": 1.5},
            )
            engine = AutoClipEngine(
                config=config,
                repo=repo,
                bus=bus,
                channels=config.channels,
                on_moment=lambda: woken.append("yes"),
            )
            view = _view(config, index.path, channel_id)
            base = origin.timestamp() + 24
            scores = await _drive(engine, view, base)
            event = queue.get_nowait()
            rows = await repo.list_clips()
            return scores, rows, event
        finally:
            await repo.close()

    scores, rows, event = asyncio.run(_run())
    assert max(scores) >= 1.5
    assert woken == ["yes"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "auto"
    assert rows[0]["status"] == "pending"
    assert rows[0]["signal_score"] >= 1.5
    breakdown = json.loads(rows[0]["breakdown_json"])
    assert "chat_velocity" in breakdown["signals"]
    assert event.payload["breakdown"]["score"] >= 1.5
    clip = Path(event.payload["path"])
    assert clip.exists()
    duration = probe_duration(clip)
    assert 15 <= duration <= 45
    stored = json.loads(rows[0]["attribution_json"])
    assert stored["channel_key"] == "youtube_Tester"
    assert "credit" not in (clip.with_suffix(".txt").read_text(encoding="utf-8") if clip.with_suffix(".txt").exists() else "")


def test_nudge_and_calibrate_apply_from_real_decisions(tmp_path: Path, capsys):
    config = _config(tmp_path)
    yaml = tmp_path / "config.yaml"
    yaml.write_text(
        "\n".join(
            [
                f'buffer_dir: "{config.buffer_dir.as_posix()}"',
                f'clips_dir: "{config.resolved_clips_dir().as_posix()}"',
                f'database_path: "{config.resolved_database_path().as_posix()}"',
                "transcript_on_trigger: false",
                "channels:",
                "  - platform: youtube",
                "    handle: Tester",
                "    authorized: true",
                "    weights: {chat: 0.35, emotes: 0.25, audio: 0.30, scene: 0.05, viewers: 0.05}",
                "    thresholds: {signal_score: 2.5, cooldown_s: 45}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    async def _seed() -> None:
        repo = SqliteRepo(config.resolved_database_path())
        await repo.init()
        try:
            channel_id = await repo.upsert_channel(
                channel_key="youtube_Tester",
                platform="youtube",
                handle="Tester",
                url=None,
                authorized=True,
                context="",
                weights={},
                thresholds={"signal_score": 2.5},
            )
            breakdown = {
                "score": 3.2,
                "signals": {
                    "chat_velocity": {"raw": 1, "z": 3.0, "weight": 0.35},
                    "audio_energy": {"raw": 0.1, "z": 0.4, "weight": 0.30},
                },
            }
            low = {
                "score": 2.1,
                "signals": {
                    "chat_velocity": {"raw": 0.2, "z": 0.4, "weight": 0.35},
                    "audio_energy": {"raw": 0.2, "z": 0.4, "weight": 0.30},
                },
            }
            clip_id = await repo.insert_clip(
                channel_id=channel_id,
                kind="auto",
                status="rejected",
                attribution={"channel_key": "youtube_Tester"},
                file_path=None,
                breakdown=low,
                signal_score=2.1,
            )
            for _ in range(3):
                await repo.insert_decision(clip_id=clip_id, action="reject", weight=1, breakdown=low)
            hot = await repo.insert_clip(
                channel_id=channel_id,
                kind="manual",
                status="approved",
                attribution={"channel_key": "youtube_Tester"},
                file_path=None,
                breakdown=breakdown,
                signal_score=3.2,
                weight=3,
            )
            await repo.insert_decision(clip_id=hot, action="approve", weight=3, note="hotkey", breakdown=breakdown)
            engine = AutoClipEngine(
                config=config,
                repo=repo,
                bus=InMemoryEventBus(),
                channels=config.channels,
            )
            view = _view(config, tmp_path / "none" / "index.json", channel_id)
            engine.ensure(view)
            await engine.learn_from_decisions()
            # The hotkey decision is stored but does not count as a review approval.
            assert engine._states[view.channel_key].nudge.threshold == round(2.5 * 1.08, 3)
        finally:
            await repo.close()

    asyncio.run(_seed())
    assert main(["calibrate", "--config", str(yaml), "--apply"]) == 0
    text = capsys.readouterr().out
    assert "youtube_Tester" in text
    saved = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert "youtube_Tester" in saved["channels"]
    assert saved["channels"]["youtube_Tester"]["weights"]["chat_velocity"] > 0
    assert calibrate_command(yaml, apply=False) == 0
