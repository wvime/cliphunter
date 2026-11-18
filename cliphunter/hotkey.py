"""Global hotkey that cuts a clip on the asyncio loop.

pynput listens on its own thread. Presses are handed to the loop with
``call_soon_threadsafe``. If pynput is missing or cannot grab the keyboard
(typical on Linux CI), ``run`` keeps going and logs a warning.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cliphunter.clip import ClipError, ClipResult, create_clip, default_clip_path
from cliphunter.jsonlog import log_event
from cliphunter.storage.repo import Repo

# A hotkey press is an explicit "this moment matters" label for later calibration.
HOTKEY_CALIBRATION_WEIGHT = 3.0

_MODIFIERS = {
    "ctrl": "<ctrl>",
    "alt": "<alt>",
    "shift": "<shift>",
    "cmd": "<cmd>",
    "win": "<cmd>",
    "super": "<cmd>",
}

ListenerFactory = Callable[[str, Callable[[], None]], object | None]
Notifier = Callable[[str, str], None]


@dataclass(frozen=True)
class ActiveStream:
    channel_key: str
    config_id: str | None
    display_name: str
    started_at: float
    index_path: Path
    session_id: int
    channel_db_id: int
    authorization: dict | None
    credit: str | None = None


def to_pynput_hotkey(spec: str) -> str:
    """Turn ``ctrl+alt+c`` into pynput's ``<ctrl>+<alt>+c``."""

    parts = [part.strip().lower() for part in spec.replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise ValueError("hotkey vacío")
    rendered: list[str] = []
    for part in parts:
        if part in _MODIFIERS:
            rendered.append(_MODIFIERS[part])
        elif len(part) == 1:
            rendered.append(part)
        else:
            rendered.append(f"<{part}>")
    return "+".join(rendered)


def select_active_stream(streams: list[ActiveStream], preferred: str | None) -> ActiveStream | None:
    """Pick the only live stream, the configured one, or the most recently started."""

    if not streams:
        return None
    if len(streams) == 1:
        return streams[0]
    needle = (preferred or "").strip().lstrip("@").lower()
    if needle:
        for stream in streams:
            aliases = {
                stream.channel_key.lower(),
                (stream.config_id or "").lower(),
                stream.display_name.lstrip("@").lower(),
            }
            if needle in aliases:
                return stream
        log_event(
            "WARNING",
            "hotkey.preferred_missing",
            active_channel=preferred,
            live=[stream.channel_key for stream in streams],
        )
    return max(streams, key=lambda stream: stream.started_at)


def notify_user(title: str, message: str) -> None:
    """Console log, plus a Windows toast when plyer or win11toast imports."""

    print(f"[cliphunter] {title}: {message}", flush=True)
    log_event("INFO", "hotkey.notify", title=title, message=message)
    if _toast(title, message):
        return
    if not notify_user.warned:  # type: ignore[attr-defined]
        notify_user.warned = True  # type: ignore[attr-defined]
        log_event(
            "WARNING",
            "hotkey.toast_unavailable",
            message="Sin plyer ni win11toast; el aviso queda solo en la consola.",
        )


notify_user.warned = False  # type: ignore[attr-defined]


def _toast(title: str, message: str) -> bool:
    try:
        from win11toast import toast  # type: ignore[import-not-found]
    except Exception:
        toast = None
    if toast is not None:
        try:
            toast(title, message, duration="short")
            return True
        except Exception as exc:
            log_event("WARNING", "hotkey.toast_failed", backend="win11toast", error=str(exc))
    try:
        from plyer import notification
    except Exception:
        return False
    try:
        notification.notify(title=title, message=message, app_name="ClipHunter", timeout=4)
        return True
    except Exception as exc:
        log_event("WARNING", "hotkey.toast_failed", backend="plyer", error=str(exc))
        return False


def pynput_listener(hotkey: str, callback: Callable[[], None]) -> object | None:
    try:
        from pynput import keyboard
    except ImportError:
        log_event("WARNING", "hotkey.unavailable", reason="pynput no está instalado")
        return None
    try:
        return keyboard.GlobalHotKeys({hotkey: callback})
    except Exception as exc:
        log_event("WARNING", "hotkey.unavailable", reason=str(exc))
        return None


class HotkeyService:
    """Debounced bridge from the pynput thread to one async callback."""

    def __init__(
        self,
        *,
        combo: str,
        debounce_s: float,
        on_fire: Callable[[], Awaitable[None]],
        listener_factory: ListenerFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.combo = combo
        self.pynput_hotkey = to_pynput_hotkey(combo)
        self.debounce_s = debounce_s
        self.on_fire = on_fire
        self.listener_factory = listener_factory or pynput_listener
        self.clock = clock
        self._last: float | None = None
        self._listener: object | None = None
        self._task: asyncio.Task[None] | None = None
        self.armed = False

    def start(self) -> bool:
        loop = asyncio.get_running_loop()

        def _pressed() -> None:
            try:
                loop.call_soon_threadsafe(self._on_loop)
            except RuntimeError as exc:
                log_event("WARNING", "hotkey.loop_closed", error=str(exc))

        try:
            listener = self.listener_factory(self.pynput_hotkey, _pressed)
        except Exception as exc:
            log_event("WARNING", "hotkey.unavailable", reason=str(exc))
            return False
        if listener is None:
            return False
        try:
            start = getattr(listener, "start", None)
            if start is not None:
                start()
        except Exception as exc:
            log_event("WARNING", "hotkey.unavailable", reason=str(exc))
            return False
        self._listener = listener
        self.armed = True
        log_event("INFO", "hotkey.armed", combo=self.combo, pynput=self.pynput_hotkey)
        return True

    def _on_loop(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.debounce_s:
            log_event("INFO", "hotkey.debounced", combo=self.combo, waited=round(now - self._last, 3))
            return
        if self._task is not None and not self._task.done():
            log_event("INFO", "hotkey.busy", combo=self.combo)
            return
        self._last = now
        self._task = asyncio.create_task(self._run(), name="hotkey-clip")

    async def _run(self) -> None:
        try:
            await self.on_fire()
        except Exception as exc:
            log_event("ERROR", "hotkey.failed", error=str(exc))

    async def aclose(self) -> None:
        listener = self._listener
        self._listener = None
        self.armed = False
        if listener is not None:
            stop = getattr(listener, "stop", None)
            if stop is not None:
                try:
                    stop()
                except Exception as exc:
                    log_event("WARNING", "hotkey.stop_failed", error=str(exc))
            join = getattr(listener, "join", None)
            if join is not None:
                try:
                    await asyncio.to_thread(join, 2)
                except Exception:
                    pass
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class HotkeyClipper:
    """Cut the active channel with the same copy-only path as ``clip``."""

    def __init__(
        self,
        *,
        streams: Callable[[], list[ActiveStream]],
        seconds: float,
        preferred: str | None,
        repo: Repo,
        clips_dir: Path,
        notifier: Notifier = notify_user,
        on_created: Callable[[ClipResult], Awaitable[None]] | None = None,
    ) -> None:
        self.streams = streams
        self.seconds = seconds
        self.preferred = preferred
        self.repo = repo
        self.clips_dir = clips_dir
        self.notifier = notifier
        self.on_created = on_created

    async def __call__(self) -> ClipResult | None:
        chosen = select_active_stream(self.streams(), self.preferred)
        if chosen is None:
            self.notifier("ClipHunter", "No hay ningún directo activo.")
            log_event("WARNING", "hotkey.no_stream")
            return None
        output = default_clip_path(self.clips_dir, chosen.channel_key)
        try:
            result = await create_clip(
                index_path=chosen.index_path,
                seconds=self.seconds,
                output=output,
                repo=self.repo,
                channel_id=chosen.channel_db_id,
                authorization=chosen.authorization,
                weight=HOTKEY_CALIBRATION_WEIGHT,
                credit=chosen.credit,
                status="pending",
            )
        except ClipError as exc:
            self.notifier("ClipHunter", str(exc))
            log_event("ERROR", "hotkey.failed", channel=chosen.channel_key, error=str(exc))
            return None
        if result.clip_id is not None:
            await self.repo.insert_decision(
                clip_id=result.clip_id,
                action="approve",
                note="hotkey",
                weight=HOTKEY_CALIBRATION_WEIGHT,
            )
        self.notifier(
            "ClipHunter",
            f"Clip de {chosen.display_name} ({self.seconds:g} s) guardado en {result.path.name}",
        )
        log_event(
            "INFO",
            "hotkey.saved",
            channel=chosen.channel_key,
            path=str(result.path),
            clip_id=result.clip_id,
            weight=HOTKEY_CALIBRATION_WEIGHT,
            kind="manual",
        )
        if self.on_created is not None:
            try:
                await self.on_created(result)
            except Exception as exc:
                log_event("WARNING", "review.wake_failed", error=str(exc))
        return result
