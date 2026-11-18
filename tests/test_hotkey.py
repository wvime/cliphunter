"""Hotkey dispatch, debounce, and which live channel gets cut."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cliphunter.clip import ClipResult
from cliphunter.config import ConfigError, load_config
from cliphunter.hotkey import (
    HOTKEY_CALIBRATION_WEIGHT,
    ActiveStream,
    HotkeyClipper,
    HotkeyService,
    notify_user,
    pynput_listener,
    select_active_stream,
    to_pynput_hotkey,
)
from cliphunter.storage.repo import SqliteRepo


def _stream(
    *,
    key: str,
    config_id: str,
    started_at: float,
    display: str | None = None,
) -> ActiveStream:
    return ActiveStream(
        channel_key=key,
        config_id=config_id,
        display_name=display or config_id,
        started_at=started_at,
        index_path=Path(f"{key}.json"),
        session_id=1,
        channel_db_id=1,
        authorization=None,
    )


def test_hotkey_string_maps_to_pynput():
    assert to_pynput_hotkey("ctrl+alt+c") == "<ctrl>+<alt>+c"
    assert to_pynput_hotkey("Ctrl-Alt-C") == "<ctrl>+<alt>+c"


def test_select_active_stream_one_recent_or_configured():
    older = _stream(key="youtube_old", config_id="old", started_at=1)
    newer = _stream(key="youtube_alofoke", config_id="alofoke", started_at=5)
    assert select_active_stream([], None) is None
    assert select_active_stream([older], "alofoke") is older
    assert select_active_stream([older, newer], None) is newer
    assert select_active_stream([older, newer], "alofoke") is newer
    assert select_active_stream([older, newer], "youtube_old") is older
    assert select_active_stream([older, newer], "@Old") is older
    assert select_active_stream([older, newer], "nadie") is newer


def test_hotkey_config_rejects_a_modifier_only_combo(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "hotkey: alt+shift\nchannels: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="hotkey"):
        load_config(path)


def test_pynput_missing_does_not_raise(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "pynput" or name.startswith("pynput."):
            raise ImportError("pynput no está")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert pynput_listener("<ctrl>+<alt>+c", lambda: None) is None


async def test_listener_callback_is_handed_to_the_loop():
    loop = asyncio.get_running_loop()
    scheduled: list[object] = []
    original = loop.call_soon_threadsafe

    def _spy(callback, *args):
        scheduled.append(callback)
        return original(callback, *args)

    loop.call_soon_threadsafe = _spy  # type: ignore[method-assign]
    fired = asyncio.Event()

    async def _on_fire() -> None:
        fired.set()

    holder: dict = {}

    def _factory(hotkey, callback):
        holder["hotkey"] = hotkey
        holder["callback"] = callback

        class _Listener:
            def start(self) -> None:
                holder["started"] = True

            def stop(self) -> None:
                holder["stopped"] = True

        return _Listener()

    service = HotkeyService(
        combo="ctrl+alt+c",
        debounce_s=3,
        on_fire=_on_fire,
        listener_factory=_factory,
    )
    try:
        assert service.start() is True
        assert holder["started"] is True
        assert holder["hotkey"] == "<ctrl>+<alt>+c"
        holder["callback"]()
        await asyncio.wait_for(fired.wait(), timeout=1)
        assert scheduled
    finally:
        loop.call_soon_threadsafe = original  # type: ignore[method-assign]
        await service.aclose()
        assert holder["stopped"] is True


async def test_debounce_ignores_presses_inside_the_window():
    now = {"t": 0.0}
    calls = 0

    async def _on_fire() -> None:
        nonlocal calls
        calls += 1

    service = HotkeyService(
        combo="ctrl+alt+c",
        debounce_s=3,
        on_fire=_on_fire,
        clock=lambda: now["t"],
        listener_factory=lambda _hotkey, _callback: None,
    )
    service._on_loop()
    await asyncio.sleep(0)
    now["t"] = 2.9
    service._on_loop()
    await asyncio.sleep(0)
    now["t"] = 3.0
    service._on_loop()
    await asyncio.sleep(0)
    assert calls == 2


async def test_start_failure_does_not_crash():
    class _Boom:
        def start(self) -> None:
            raise RuntimeError("no hay sesión gráfica")

    service = HotkeyService(
        combo="ctrl+alt+c",
        debounce_s=3,
        on_fire=_noop,
        listener_factory=lambda _hotkey, _callback: _Boom(),
    )
    assert service.start() is False
    assert service.armed is False


async def _noop() -> None:
    return None


async def test_dispatch_saves_manual_clip_with_calibration_weight(tmp_path: Path, monkeypatch):
    async with SqliteRepo(tmp_path / "cliphunter.sqlite") as repo:
        channel_id = await repo.upsert_channel(
            channel_key="youtube_alofoke",
            platform="youtube",
            handle=None,
            url="https://www.youtube.com/@Alofokeradioshow",
            authorized=True,
            context="idioma: es",
            weights={},
            thresholds={},
        )
        stream = ActiveStream(
            channel_key="youtube_alofoke",
            config_id="alofoke",
            display_name="alofoke",
            started_at=5,
            index_path=tmp_path / "index.json",
            session_id=1,
            channel_db_id=channel_id,
            authorization={"tipo": "permiso público del creador", "fecha": "2026-11-17"},
        )
        captured: dict = {}

        async def _fake_create_clip(**kwargs):
            captured.update(kwargs)
            clip_id = await repo.insert_clip(
                channel_id=kwargs["channel_id"],
                kind="manual",
                status="ready",
                attribution={"calibration_weight": kwargs["weight"], **(kwargs.get("authorization") or {})},
                file_path=str(kwargs["output"]),
                weight=kwargs["weight"],
                duration_seconds=kwargs["seconds"],
            )
            return ClipResult(
                path=kwargs["output"],
                sidecar=kwargs["output"].with_suffix(".json"),
                attribution={"calibration_weight": kwargs["weight"]},
                clip_id=clip_id,
                duration=kwargs["seconds"],
            )

        monkeypatch.setattr("cliphunter.hotkey.create_clip", _fake_create_clip)
        notes: list[str] = []
        clipper = HotkeyClipper(
            streams=lambda: [stream],
            seconds=45,
            preferred="alofoke",
            repo=repo,
            clips_dir=tmp_path / "clips",
            notifier=lambda _title, message: notes.append(message),
        )
        result = await clipper()
        assert result is not None
        assert captured["seconds"] == 45
        assert captured["weight"] == HOTKEY_CALIBRATION_WEIGHT
        assert captured["index_path"] == stream.index_path
        assert captured["authorization"]["tipo"] == "permiso público del creador"
        stored = await repo.list_clips(channel_id)
        assert stored[0]["kind"] == "manual"
        assert stored[0]["weight"] == HOTKEY_CALIBRATION_WEIGHT
        decisions = await repo.list_decisions(result.clip_id)
        assert decisions[0]["action"] == "approve"
        assert decisions[0]["note"] == "hotkey"
        assert decisions[0]["weight"] == HOTKEY_CALIBRATION_WEIGHT
        assert notes and "alofoke" in notes[0]


async def test_dispatch_without_a_live_stream_does_not_cut(tmp_path: Path, monkeypatch):
    async with SqliteRepo(tmp_path / "cliphunter.sqlite") as repo:
        called = False

        async def _fake_create_clip(**_kwargs):
            nonlocal called
            called = True
            raise AssertionError("no debería cortar")

        monkeypatch.setattr("cliphunter.hotkey.create_clip", _fake_create_clip)
        notes: list[str] = []
        clipper = HotkeyClipper(
            streams=lambda: [],
            seconds=45,
            preferred=None,
            repo=repo,
            clips_dir=tmp_path / "clips",
            notifier=lambda _title, message: notes.append(message),
        )
        assert await clipper() is None
        assert called is False
        assert notes == ["No hay ningún directo activo."]


def test_notify_survives_missing_toast_libraries(monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name in {"win11toast", "plyer"} or name.startswith("plyer."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    notify_user.warned = False  # type: ignore[attr-defined]
    notify_user("ClipHunter", "hola")
    assert "hola" in capsys.readouterr().out
