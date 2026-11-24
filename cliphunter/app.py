"""Wire config, confirmation, watcher, ingest and metrics into one process."""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass

import httpx

from cliphunter.authprompt import AuthorizationRequired, collect_confirmations, resolve_prompt_mode
from cliphunter.bus import InMemoryEventBus
from cliphunter.config import AppConfig, ChannelConfig, channel_key
from cliphunter.env import EnvSettings
from cliphunter.hotkey import HotkeyClipper, HotkeyService
from cliphunter.review.service import build_review_runtime
from cliphunter.jsonlog import log_event, set_level
from cliphunter.manager import IngestManager
from cliphunter.observability import MetricsSampler
from cliphunter.platforms import build_platforms
from cliphunter.signals.engine import AutoClipEngine, LiveView
from cliphunter.storage.repo import SqliteRepo
from cliphunter.watcher import Watcher

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class RunPlan:
    channels: list[ChannelConfig]
    confirmations: dict[str, str]


def prepare_run(config: AppConfig, *, skip_flag: bool) -> RunPlan:
    """Ask for authorization before any recording. Safe to call outside the event loop."""

    set_level(config.log_level)
    authorized = config.authorized_channels()
    if not authorized:
        raise AuthorizationRequired(
            "Ningún canal tiene authorized: true. Edita config.yaml y vuelve a lanzar run."
        )
    mode = resolve_prompt_mode(
        skip_flag=skip_flag,
        config_flag=config.skip_authorization_prompt,
    )
    confirmations = collect_confirmations(authorized, mode)
    channels = [channel for channel in authorized if channel_key(channel) in confirmations]
    if not channels:
        raise AuthorizationRequired("No confirmaste ningún canal. No se graba nada.")
    return RunPlan(channels=channels, confirmations=confirmations)


async def run_app(
    config: AppConfig,
    env: EnvSettings,
    plan: RunPlan,
    *,
    hotkey_enabled: bool = True,
) -> int:
    set_level(config.log_level)
    config.buffer_dir.mkdir(parents=True, exist_ok=True)
    config.resolved_clips_dir().mkdir(parents=True, exist_ok=True)
    repo = SqliteRepo(config.resolved_database_path())
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )
    stop = asyncio.Event()
    try:
        await repo.init()
        interrupted = await repo.interrupt_open_sessions()
        if interrupted:
            log_event("WARNING", "sessions.interrupted", count=interrupted)
        channel_ids: dict[str, int] = {}
        for channel in plan.channels:
            key = channel_key(channel)
            channel_ids[key] = await repo.upsert_channel(
                channel_key=key,
                platform=channel.platform,
                handle=channel.handle,
                url=channel.url,
                authorized=channel.authorized,
                context=channel.context,
                weights=channel.weights,
                thresholds=channel.thresholds,
            )
            if channel.platform == "kick":
                log_event(
                    "WARNING",
                    "kick.stub",
                    channel=key,
                    message="Kick no está implementado en la fase 1; no se grabará.",
                )
        if any(channel.platform == "twitch" for channel in plan.channels):
            if not env.twitch_client_id or not env.twitch_client_secret:
                log_event(
                    "WARNING",
                    "twitch.missing_credentials",
                    message="Sin TWITCH_CLIENT_ID y TWITCH_CLIENT_SECRET no se detectan directos de Twitch.",
                )
        bus = InMemoryEventBus()
        platforms = build_platforms(env, client=client)
        watcher = Watcher(
            channels=plan.channels,
            platforms=platforms,
            bus=bus,
            interval=float(config.poll_interval_seconds),
        )
        manager = IngestManager(
            bus=bus,
            repo=repo,
            channel_ids=channel_ids,
            confirmations=plan.confirmations,
            buffer_dir=config.buffer_dir,
            segment_seconds=config.segment_seconds,
            buffer_minutes=config.buffer_minutes,
            qualities=config.quality,
            max_concurrent=config.max_concurrent_streams,
        )
        metrics = MetricsSampler(
            repo=repo,
            interval=float(config.metrics_interval_seconds),
            streams=manager.list_streams,
            bus=bus,
        )
        _install_signals(stop)
        review = build_review_runtime(repo=repo, config=config, env=env, enable_local=True)
        review_task = asyncio.create_task(review.run(stop), name="review")
        auto = AutoClipEngine(
            config=config,
            repo=repo,
            bus=bus,
            channels=plan.channels,
            on_moment=review.wake,
        )
        from cliphunter.editorial.runtime import EditorialDirector, editorial_active

        if editorial_active(config, env):
            auto.editorial = EditorialDirector(config, env, repo, auto)
            log_event(
                "INFO",
                "editorial.enabled",
                model=config.editorial_model,
                claude=bool(env.anthropic_api_key),
            )
        auto_task = asyncio.create_task(
            _auto_loop(auto, manager, plan.channels, config, stop),
            name="auto-clip",
        )
        hotkey: HotkeyService | None = None
        if hotkey_enabled:
            clipper = HotkeyClipper(
                streams=manager.live_streams,
                seconds=float(config.hotkey_seconds),
                preferred=config.active_channel,
                repo=repo,
                clips_dir=config.resolved_clips_dir(),
                on_created=lambda _result: _wake(review),
            )
            hotkey = HotkeyService(
                combo=config.hotkey,
                debounce_s=float(config.hotkey_debounce_seconds),
                on_fire=clipper,
            )
            if not hotkey.start():
                log_event(
                    "WARNING",
                    "hotkey.disabled",
                    reason="no se pudo registrar el atajo; la grabación sigue sin él",
                )
        else:
            log_event("INFO", "hotkey.disabled", reason="--no-hotkey")
        log_event(
            "INFO",
            "app.start",
            channels=[channel_key(channel) for channel in plan.channels],
            max_concurrent_streams=config.max_concurrent_streams,
            buffer_dir=str(config.buffer_dir),
            buffer_minutes=config.buffer_minutes,
            segment_seconds=config.segment_seconds,
            poll_interval_seconds=config.poll_interval_seconds,
            hotkey=config.hotkey if hotkey_enabled else None,
            hotkey_seconds=config.hotkey_seconds,
            auto_clip=config.auto_clip,
            database=str(config.resolved_database_path()),
            youtube_api_configured=bool(env.youtube_api_key),
            twitch_configured=bool(env.twitch_client_id and env.twitch_client_secret),
            anthropic_configured=bool(env.anthropic_api_key),
        )
        manager_task = asyncio.create_task(_supervise("manager", manager.run(stop), stop), name="manager")
        await asyncio.wait_for(manager.ready.wait(), timeout=10)
        watcher_task = asyncio.create_task(_supervise("watcher", watcher.run(stop), stop), name="watcher")
        metrics_task = asyncio.create_task(_supervise("metrics", metrics.run(stop), stop), name="metrics")
        try:
            await stop.wait()
        finally:
            if hotkey is not None:
                await hotkey.aclose()
            stop.set()
            auto_task.cancel()
            review_task.cancel()
            watcher_task.cancel()
            metrics_task.cancel()
            await asyncio.gather(auto_task, review_task, watcher_task, metrics_task, return_exceptions=True)
            await manager_task
        log_event("INFO", "app.stop")
    finally:
        await client.aclose()
        await repo.close()
    return 0


async def _auto_loop(engine: AutoClipEngine, manager: IngestManager, channels: list[ChannelConfig], config: AppConfig, stop: asyncio.Event) -> None:
    """Score live buffers. A detector failure is logged and the loop continues."""

    if not config.auto_clip:
        log_event("INFO", "auto.disabled")
        return
    by_key = {channel_key(channel): channel for channel in channels}
    cycle = 0
    while not stop.is_set():
        started = time.perf_counter()
        live = manager.live_streams()
        try:
            for stream in live:
                channel = by_key.get(stream.channel_key)
                view = LiveView(
                    channel_key=stream.channel_key,
                    channel_db_id=stream.channel_db_id,
                    index_path=stream.index_path,
                    video_id=stream.video_id,
                    config_id=stream.config_id,
                    emotes=list(channel.emotes_hype) if channel else [],
                    weights=dict(channel.weights) if channel else {},
                    thresholds=dict(channel.thresholds) if channel else {},
                    credit=stream.credit,
                    authorization=stream.authorization,
                )
                await engine.tick(view, time.time())
            await engine.learn_from_decisions()
        except Exception as exc:
            log_event("ERROR", "auto.tick_failed", error=str(exc))
        elapsed_ms = (time.perf_counter() - started) * 1000
        cycle += 1
        if cycle == 1 or cycle % 15 == 0:
            log_event(
                "INFO",
                "signal.cycle",
                elapsed_ms=round(elapsed_ms, 1),
                streams=len(live),
                channels=[stream.channel_key for stream in live],
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(config.signal_interval_seconds))
        except TimeoutError:
            continue


async def _wake(review: object) -> None:
    wake = getattr(review, "wake", None)
    if wake is not None:
        wake()


async def _supervise(name: str, coro: object, stop: asyncio.Event) -> None:
    try:
        await coro  # type: ignore[misc]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_event("ERROR", "task.crashed", task=name, error=str(exc))
        stop.set()
        raise


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _set() -> None:
        if not stop.is_set():
            log_event("INFO", "shutdown.requested")
            stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _set)
        except NotImplementedError:
            if sig == signal.SIGINT:
                signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(_set))
