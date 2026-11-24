"""Review queue with the Telegram API mocked."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from cliphunter.config import AppConfig
from cliphunter.env import EnvSettings
from cliphunter.index import Segment, SegmentIndex
from cliphunter.review.adjust import shift_window
from cliphunter.review.localapp import build_review_app
from cliphunter.review.messages import build_caption, parse_callback, ready_sidecar_text, review_keyboard
from cliphunter.review.preview import TELEGRAM_VIDEO_LIMIT, compress_preview_command, needs_compress
from cliphunter.review.service import ReviewRuntime
from cliphunter.review.state import load_state, save_state, state_path
from cliphunter.review.telegram import TelegramNotifier, wait_for_start
from cliphunter.storage.repo import SqliteRepo
from cliphunter.timeutil import utc_iso, utc_now


class FakeTelegram:
    def __init__(self) -> None:
        self.history: list[dict] = []
        self.updates: list[dict] = []
        self.fail_next = False

    async def __call__(self, method, *, data=None, json_body=None, files=None):
        if self.fail_next and method == "getUpdates":
            self.fail_next = False
            raise ConnectionError("telegram caído")
        self.history.append({"method": method, "data": data, "json": json_body, "files": files})
        if method == "getUpdates":
            batch = list(self.updates)
            self.updates.clear()
            return {"ok": True, "result": batch}
        if method == "sendVideo":
            return {"ok": True, "result": {"message_id": len(self.history)}}
        return {"ok": True, "result": True}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        buffer_dir=tmp_path / "buffer",
        database_path=tmp_path / "cliphunter.sqlite",
        clips_dir=tmp_path / "clips",
        channels=[],
    )


def _segment(name: str, duration: float = 4) -> Segment:
    now = utc_now()
    return Segment(
        file=name,
        duration=duration,
        wall_start=utc_iso(now),
        wall_end=utc_iso(now),
        streamer="alofoke",
        platform="youtube",
    )


async def _pending_clip(repo: SqliteRepo, tmp_path: Path, *, channel_id: int, breakdown: dict | None = None) -> int:
    video = tmp_path / "clips" / "show.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"fake-mp4")
    attribution = {
        "streamer": "alofoke",
        "platform": "youtube",
        "clip_start": "2026-11-18T00:00:00+00:00",
        "clip_end": "2026-11-18T00:00:45+00:00",
        "credit": "🎙️ @Alofokeradioshow",
        "source_segments": ["b.ts", "c.ts"],
        "buffer_index": str(tmp_path / "buffer" / "index.json"),
        "breakdown": breakdown,
    }
    return await repo.insert_clip(
        channel_id=channel_id,
        kind="manual",
        status="pending",
        attribution=attribution,
        file_path=str(video),
        breakdown=breakdown,
        signal_score=2.5 if breakdown else None,
        duration_seconds=8,
    )


def test_caption_keyboard_and_sidecar():
    caption = build_caption(
        {
            "channel": "alofoke",
            "clip_start": "2026-11-18T00:00:00+00:00",
            "clip_end": "2026-11-18T00:00:45+00:00",
            "signal_score": 2.5,
            "breakdown": {"chat": 0.4},
        }
    )
    assert "Canal: alofoke" in caption
    assert "Inicio: 2026-11-18T00:00:00+00:00" in caption
    assert "Score: 2.5" in caption
    assert "chat" in caption
    labels = [button["text"] for row in review_keyboard(4)["inline_keyboard"] for button in row]
    assert labels == [
        "Aprobar",
        "Rechazar",
        "Inicio -5 s",
        "Inicio +5 s",
        "Final -5 s",
        "Final +5 s",
    ]
    assert parse_callback("why:4:aburrido") == ("why:aburrido", 4)
    assert parse_callback("is:4") == ("is", 4)
    text = ready_sidecar_text(
        {
            "title": "se cayó el micrófono",
            "hook": "nadie lo vio venir",
            "hashtags": ["#radio"],
            "credit": "🎙️ @Alofokeradioshow",
        }
    )
    assert text == "title: se cayó el micrófono\nhook: nadie lo vio venir\nhashtags: #radio\n"
    assert "@Alofokeradioshow" not in text
    assert "🎙️" not in text


def test_compress_only_above_telegram_limit(tmp_path: Path):
    assert needs_compress(tmp_path / "x.mp4", size=100) is False
    assert needs_compress(tmp_path / "x.mp4", size=TELEGRAM_VIDEO_LIMIT + 1) is True
    command = compress_preview_command(tmp_path / "in.mp4", tmp_path / "out.mp4")
    assert command[command.index("-threads") + 1] == "1"
    assert "scale=-2:480" in command
    assert "libx264" in command


def test_shift_window_moves_whole_segments(tmp_path: Path):
    index = SegmentIndex(channel_key="youtube_alofoke", platform="youtube", directory=tmp_path)
    index.segments = [_segment(name) for name in ("a.ts", "b.ts", "c.ts", "d.ts", "e.ts")]
    earlier = shift_window(index, ["b.ts", "c.ts"], edge="inicio", delta_seconds=-5)
    assert [segment.file for segment in earlier] == ["a.ts", "b.ts", "c.ts"]
    later_start = shift_window(index, ["a.ts", "b.ts", "c.ts"], edge="inicio", delta_seconds=5)
    assert [segment.file for segment in later_start] == ["c.ts"]
    shorter = shift_window(index, ["b.ts", "c.ts", "d.ts"], edge="fin", delta_seconds=-5)
    assert shorter[-1].file == "b.ts"
    longer = shift_window(index, ["b.ts"], edge="fin", delta_seconds=5)
    assert [segment.file for segment in longer] == ["b.ts", "c.ts", "d.ts"]


async def test_telegram_setup_stores_start_chat(tmp_path: Path):
    api = FakeTelegram()
    api.updates = [
        {"update_id": 3, "message": {"text": "/start", "chat": {"id": 4242, "username": "owner"}, "from": {}}}
    ]
    path = tmp_path / "telegram_chat.json"
    chat_id = await wait_for_start(api, path)
    assert chat_id == 4242
    assert load_state(path).chat_id == 4242
    assert load_state(path).offset == 4


async def test_send_preview_and_ignore_other_chats(tmp_path: Path):
    config = _config(tmp_path)
    save_state(state_path(config), load_state(state_path(config)).__class__(chat_id=7, offset=1))
    async with SqliteRepo(config.resolved_database_path()) as repo:
        channel_id = await _channel(repo)
        clip_id = await _pending_clip(repo, tmp_path, channel_id=channel_id, breakdown={"chat": 0.4})
        api = FakeTelegram()
        runtime = ReviewRuntime(
            repo=repo,
            config=config,
            env=EnvSettings(telegram_bot_token="secret-token"),
            transport=api,
            enable_local=False,
        )
        await runtime.scan_once()
        send = next(call for call in api.history if call["method"] == "sendVideo")
        assert "Canal: alofoke" in send["data"]["caption"]
        assert "Aprobar" in send["data"]["reply_markup"]
        assert send["data"]["chat_id"] == "7"
        assert send["files"]["video"].name == "show.mp4"
        calls = []

        async def _record(clip, code):
            calls.append((clip, code))

        notifier = TelegramNotifier("secret-token", state_path(config), _record, transport=api)
        await notifier._handle(
            {"callback_query": {"id": "q1", "data": "a:1", "message": {"chat": {"id": 99}}}},
            load_state(state_path(config)),
        )
        assert calls == []
        answered = [call for call in api.history if call["method"] == "answerCallbackQuery"]
        assert answered[-1]["json"]["text"] == "Este chat no está autorizado."
        assert clip_id == 1


async def test_approve_keeps_credit_out_of_the_publishable_text(tmp_path: Path):
    config = _config(tmp_path)
    async with SqliteRepo(config.resolved_database_path()) as repo:
        channel_id = await _channel(repo)
        clip_id = await _pending_clip(repo, tmp_path, channel_id=channel_id, breakdown={"audio": 0.3})
        runtime = ReviewRuntime(repo=repo, config=config, env=EnvSettings(), enable_local=False)
        dest = await runtime.approve(clip_id)
        assert dest.exists()
        sidecar = dest.with_suffix(".txt").read_text(encoding="utf-8")
        assert sidecar == "title: \nhook: \nhashtags: \n"
        assert "@Alofokeradioshow" not in sidecar
        row = await repo.get_clip(clip_id)
        assert row is not None
        assert row["status"] == "approved"
        stored = json.loads(row["attribution_json"])
        assert stored["credit"] == "🎙️ @Alofokeradioshow"
        decisions = await repo.list_decisions(clip_id)
        assert decisions[0]["action"] == "approve"
        assert json.loads(decisions[0]["breakdown_json"]) == {"audio": 0.3}


async def test_adjust_recuts_and_resends(tmp_path: Path):
    config = _config(tmp_path)
    save_state(state_path(config), type(load_state(state_path(config)))(chat_id=7))
    async with SqliteRepo(config.resolved_database_path()) as repo:
        channel_id = await _channel(repo)
        clip_id = await _pending_clip(repo, tmp_path, channel_id=channel_id, breakdown={"chat": 1})
        seen: dict = {}

        async def _recut(index_path, files, edge, delta, output, previous):
            seen.update(edge=edge, delta=delta, files=list(files))
            output.write_bytes(b"adjusted")
            attribution = dict(previous)
            attribution["source_segments"] = ["a.ts", "b.ts", "c.ts"]
            attribution["clip_start"] = "earlier"
            attribution["clip_end"] = "same"
            return {"attribution": attribution, "duration": 12, "path": output}

        api = FakeTelegram()
        runtime = ReviewRuntime(
            repo=repo,
            config=config,
            env=EnvSettings(telegram_bot_token="secret-token"),
            transport=api,
            recut=_recut,
            enable_local=False,
        )
        await runtime.adjust(clip_id, "inicio", -5)
        assert seen == {"edge": "inicio", "delta": -5, "files": ["b.ts", "c.ts"]}
        decisions = await repo.list_decisions(clip_id)
        assert decisions[0]["action"] == "adjust"
        assert decisions[0]["adjust_seconds"] == -5
        assert decisions[0]["note"] == "inicio"
        assert json.loads(decisions[0]["breakdown_json"]) == {"chat": 1}
        assert any(call["method"] == "sendVideo" for call in api.history)
        stored = await repo.get_clip(clip_id)
        assert stored is not None
        assert stored["status"] == "pending"
        assert "a.ts" in stored["attribution_json"]


async def test_telegram_outage_does_not_raise(tmp_path: Path):
    import asyncio

    config = _config(tmp_path)
    save_state(state_path(config), type(load_state(state_path(config)))(chat_id=7))
    api = FakeTelegram()
    api.fail_next = True
    notifier = TelegramNotifier("secret-token", state_path(config), _unused, transport=api)
    stop = asyncio.Event()

    async def _run() -> None:
        await notifier.run(stop)

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def _unused(_clip_id: int, _code: str) -> None:
    return None


async def test_local_page_approves(tmp_path: Path):
    config = _config(tmp_path)
    async with SqliteRepo(config.resolved_database_path()) as repo:
        channel_id = await _channel(repo)
        clip_id = await _pending_clip(repo, tmp_path, channel_id=channel_id)
        runtime = ReviewRuntime(repo=repo, config=config, env=EnvSettings(), enable_local=False)
        client = TestClient(build_review_app(runtime))
        home = client.get("/")
        assert home.status_code == 200
        assert "Aprobar" in home.text
        assert "Final -5 s" in home.text
        assert "aburrido" in home.text
        response = client.post("/decision", data={"clip_id": str(clip_id), "op": "a"}, follow_redirects=False)
        assert response.status_code == 303
        row = await repo.get_clip(clip_id)
        assert row is not None
        assert row["status"] == "approved"


async def _channel(repo: SqliteRepo) -> int:
    return await repo.upsert_channel(
        channel_key="youtube_alofoke",
        platform="youtube",
        handle=None,
        url="https://www.youtube.com/@Alofokeradioshow",
        authorized=True,
        context="es",
        weights={},
        thresholds={},
    )
