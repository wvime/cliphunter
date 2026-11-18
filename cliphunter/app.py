"""Wire config, confirmation, watcher, ingest and metrics into one process."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

import httpx

from cliphunter.authprompt import AuthorizationRequired, collect_confirmations, resolve_prompt_mode
from cliphunter.bus import InMemoryEventBus
from cliphunter.config import AppConfig, ChannelConfig, channel_key
from cliphunter.env import EnvSettings
from cliphunter.jsonlog import log_event, set_level
from cliphunter.manager import IngestManager
from cliphunter.observability import MetricsSampler
from cliphunter.platforms import build_platforms
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


async def run_app(config: AppConfig, env: EnvSettings, plan: RunPlan) -> int:
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
        log_event(
            "INFO",
            "app.start",
            channels=[channel_key(channel) for channel in plan.channels],
            max_concurrent_streams=config.max_concurrent_streams,
            buffer_dir=str(config.buffer_dir),
            buffer_minutes=config.buffer_minutes,
            segment_seconds=config.segment_seconds,
            poll_interval_seconds=config.poll_interval_seconds,
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
            stop.set()
            watcher_task.cancel()
            metrics_task.cancel()
            await asyncio.gather(watcher_task, metrics_task, return_exceptions=True)
            await manager_task
        log_event("INFO", "app.stop")
    finally:
        await client.aclose()
        await repo.close()
    return 0


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
