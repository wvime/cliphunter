"""Start and stop ingest tasks from stream.start / stream.end.

At most ``max_concurrent`` streams record at once. Extra live channels wait
and take a free slot when one ends. One stream's crash does not cancel the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from cliphunter.bus import SEGMENT_READY, STREAM_END, STREAM_START, Event, EventBus
from cliphunter.index import Segment
from cliphunter.ingest import IngestSession, build_streamlink_command
from cliphunter.jsonlog import log_event
from cliphunter.storage.repo import Repo
from cliphunter.timeutil import utc_iso

CommandBuilder = Callable[[dict, list[str]], list[str]]


class IngestManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        repo: Repo,
        channel_ids: dict[str, int],
        confirmations: dict[str, str],
        buffer_dir: Path,
        segment_seconds: int,
        buffer_minutes: int,
        qualities: list[str],
        max_concurrent: int,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self.bus = bus
        self.repo = repo
        self.channel_ids = channel_ids
        self.confirmations = confirmations
        self.buffer_dir = buffer_dir
        self.segment_seconds = segment_seconds
        self.buffer_minutes = buffer_minutes
        self.qualities = list(qualities)
        self.max_concurrent = max_concurrent
        self.command_builder = command_builder
        self.ready = asyncio.Event()
        self.active: dict[str, IngestSession] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.pending: list[dict] = []
        self._closing = False

    def list_streams(self) -> list[dict]:
        return [
            {
                "session_id": session.session_id,
                "channel_key": key,
                "pids": list(session.current_pids),
            }
            for key, session in self.active.items()
        ]

    async def run(self, stop: asyncio.Event) -> None:
        queue = await self.bus.subscribe(STREAM_START, STREAM_END)
        self.ready.set()
        getter = asyncio.create_task(queue.get())
        stopper = asyncio.create_task(stop.wait())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {getter, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done and getter not in done:
                    break
                event = getter.result()
                getter = asyncio.create_task(queue.get())
                try:
                    if event.type == STREAM_START:
                        await self._on_start(event.payload)
                    elif event.type == STREAM_END:
                        await self._on_end(event.payload)
                except Exception as exc:
                    log_event(
                        "ERROR",
                        "manager.event_failed",
                        error=str(exc),
                        event_type=event.type,
                    )
        finally:
            if not getter.done():
                getter.cancel()
            await asyncio.gather(getter, return_exceptions=True)
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        sessions = list(self.active.items())
        for _key, session in sessions:
            await session.stop()
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key, session in sessions:
            try:
                await self.repo.end_session(session.session_id, status="ended")
            except Exception as exc:
                log_event("ERROR", "manager.end_failed", channel=key, error=str(exc))
        self.active.clear()
        self.tasks.clear()
        self.pending.clear()

    async def _on_start(self, payload: dict) -> None:
        key = str(payload.get("channel_key") or "")
        if not key:
            return
        if key in self.active or any(item.get("channel_key") == key for item in self.pending):
            return
        if key not in self.confirmations or key not in self.channel_ids:
            log_event("WARNING", "manager.unknown_channel", channel=key)
            return
        if not payload.get("stream_url"):
            log_event("ERROR", "manager.missing_url", channel=key)
            return
        if len(self.active) >= self.max_concurrent:
            self.pending.append(payload)
            log_event(
                "WARNING",
                "ingest.deferred",
                channel=key,
                active=len(self.active),
                max_concurrent=self.max_concurrent,
            )
            return
        await self._spawn(payload)

    async def _on_end(self, payload: dict) -> None:
        key = str(payload.get("channel_key") or "")
        self.pending = [item for item in self.pending if item.get("channel_key") != key]
        session = self.active.get(key)
        if session is None:
            await self._promote()
            return
        await session.stop()
        task = self.tasks.get(key)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=15)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.active.pop(key, None)
        await self.repo.end_session(session.session_id, status="ended")
        log_event("INFO", "ingest.stopped", channel=key, session_id=session.session_id)
        await self._promote()

    async def _promote(self) -> None:
        while self.pending and len(self.active) < self.max_concurrent and not self._closing:
            payload = self.pending.pop(0)
            await self._spawn(payload)

    async def _spawn(self, payload: dict) -> None:
        key = str(payload["channel_key"])
        channel_id = self.channel_ids[key]
        confirmed = self.confirmations[key]
        session_id = await self.repo.start_session(
            channel_id=channel_id,
            stream_url=payload.get("stream_url"),
            stream_title=payload.get("title"),
            video_id=payload.get("video_id"),
            confirmed_authorized_at=confirmed,
        )
        meta = {
            "session_id": session_id,
            "channel_id": channel_id,
            "streamer": payload.get("display_name"),
            "platform": payload.get("platform"),
            "stream_url": payload.get("stream_url"),
            "stream_title": payload.get("title"),
            "context": payload.get("context") or "",
            "started_at": utc_iso(),
            "confirmed_authorized_at": confirmed,
            "video_id": payload.get("video_id"),
        }
        stream_url = str(payload["stream_url"])
        qualities = self.qualities
        builder = self.command_builder

        def factory() -> list[str]:
            if builder is not None:
                return builder(payload, qualities)
            return build_streamlink_command(stream_url, qualities)

        async def on_segment(segment: Segment) -> None:
            await self.bus.publish(
                Event(
                    SEGMENT_READY,
                    {
                        "channel_key": key,
                        "session_id": session_id,
                        "segment": segment.to_dict(),
                    },
                )
            )

        session = IngestSession(
            channel_key=key,
            directory=self.buffer_dir / key,
            segment_seconds=self.segment_seconds,
            buffer_minutes=self.buffer_minutes,
            source_cmd_factory=factory,
            session_meta=meta,
            platform=str(payload.get("platform") or ""),
            on_segment=on_segment,
        )
        self.active[key] = session
        self.tasks[key] = asyncio.create_task(self._guard(key, session), name=f"ingest-{key}")
        log_event("INFO", "ingest.started", channel=key, session_id=session_id, url=stream_url)

    async def _guard(self, key: str, session: IngestSession) -> None:
        try:
            await session.run()
        except asyncio.CancelledError:
            await session.stop()
            raise
        except Exception as exc:
            log_event("ERROR", "ingest.crashed", channel=key, error=str(exc))
        finally:
            self.tasks.pop(key, None)
