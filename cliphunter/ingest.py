"""Per-stream ingest: streamlink stdout piped into ffmpeg segment copy.

Each live channel runs in its own task. A failure only backs off that task.
Segments are MPEG-TS (copy, no decode) plus ``index.json`` so another process
can cut a clip while this one is still recording.

Precision: ``-c copy`` and ``-break_non_keyframes 0`` split on keyframes, so a
4 second target is approximate. The file ffmpeg is still writing is not indexed.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from cliphunter.backoff import ExponentialBackoff
from cliphunter.index import (
    Segment,
    SegmentIndex,
    apply_retention,
    parse_segment_csv,
    wall_window,
)
from cliphunter.jsonlog import log_event, redact
from cliphunter.procutil import drain_lines, pump_stdout, terminate_process
from cliphunter.timeutil import utc_now

SourceFactory = Callable[[], list[str]]
SegmentCallback = Callable[[Segment], Awaitable[None]]


def streamlink_quality(qualities: list[str]) -> str:
    parts: list[str] = []
    for quality in qualities:
        if quality and quality not in parts:
            parts.append(quality)
    if "worst" not in parts:
        parts.append("worst")
    return ",".join(parts)


def build_streamlink_command(stream_url: str, qualities: list[str]) -> list[str]:
    """Low-overhead streamlink invocation.

    ``--hls-live-edge 2`` keeps a short HLS buffer (less RAM).
    ``--stream-segment-threads 1`` avoids parallel segment downloads.
    ``--retry-max 2`` stops streamlink from retrying forever; ClipHunter backs off.
    ``worst`` is the last fallback so a missing 480p/720p does not jump to 1080p.
    """

    return [
        "streamlink",
        "--stdout",
        "--loglevel",
        "warning",
        "--hls-live-edge",
        "2",
        "--stream-segment-threads",
        "1",
        "--retry-open",
        "2",
        "--retry-streams",
        "1",
        "--retry-max",
        "2",
        stream_url,
        streamlink_quality(qualities),
    ]


def ffmpeg_segment_command(
    pattern: Path,
    playlist: Path,
    segment_seconds: int,
    *,
    input_format: str | None = "mpegts",
) -> list[str]:
    """Remux the pipe into ``segment_seconds`` MPEG-TS chunks without re-encoding."""

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-fflags",
        "+genpts+discardcorrupt",
    ]
    if input_format:
        command.extend(["-f", input_format])
    command.extend(
        [
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-flush_packets",
            "1",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            "-break_non_keyframes",
            "0",
            "-segment_list",
            playlist.as_posix(),
            "-segment_list_type",
            "csv",
            "-segment_list_size",
            "0",
            pattern.as_posix(),
        ]
    )
    return command


class IngestSession:
    """One live stream. ``reconnect=False`` runs a single pass (tests, finite files)."""

    def __init__(
        self,
        *,
        channel_key: str,
        directory: Path,
        segment_seconds: int,
        buffer_minutes: int,
        source_cmd_factory: SourceFactory,
        session_meta: dict,
        platform: str,
        on_segment: SegmentCallback | None = None,
        reconnect: bool = True,
        input_format: str | None = "mpegts",
    ) -> None:
        self.channel_key = channel_key
        self.directory = directory
        self.segment_seconds = segment_seconds
        self.buffer_minutes = buffer_minutes
        self.source_cmd_factory = source_cmd_factory
        self.session_meta = dict(session_meta)
        self.platform = platform
        self.on_segment = on_segment
        self.reconnect = reconnect
        self.input_format = input_format
        self.session_id = int(session_meta["session_id"]) if session_meta.get("session_id") else 0
        self.started_at = 0.0
        self.config_id: str | None = None
        self.current_pids: list[int] = []
        self._stop = asyncio.Event()
        self._proc_lock = asyncio.Lock()
        self._source: asyncio.subprocess.Process | None = None
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._generation = 0
        self._seen: set[str] = set()
        self._ignored: set[str] = set()
        self.index: SegmentIndex | None = None

    async def stop(self) -> None:
        self._stop.set()
        await self._terminate_children()

    async def run(self) -> None:
        backoff = ExponentialBackoff()
        while not self._stop.is_set():
            backoff.mark_attempt_started()
            code = 1
            try:
                code = await self.run_once()
            except asyncio.CancelledError:
                self._stop.set()
                await self._terminate_children()
                raise
            except Exception as exc:
                log_event(
                    "ERROR",
                    "ingest.attempt_failed",
                    channel=self.channel_key,
                    error=str(exc),
                )
                code = 1
            backoff.mark_attempt_finished()
            if self._stop.is_set() or not self.reconnect:
                break
            delay = backoff.next_delay()
            log_event(
                "WARNING",
                "ingest.reconnect",
                channel=self.channel_key,
                delay_seconds=round(delay, 2),
                exit_code=code,
                attempt=backoff.attempt,
            )
            if await self._wait(delay):
                break

    async def run_once(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.index is None:
            self.index = SegmentIndex.open(
                self.directory,
                channel_key=self.channel_key,
                platform=self.platform,
                session=self.session_meta,
            )
            self._seen = {segment.file for segment in self.index.segments}
        else:
            self.index.session = dict(self.session_meta)
        generation = self._alloc_generation()
        pattern = self.directory / f"g{generation:03d}_%05d.ts"
        playlist = self.directory / f"g{generation:03d}.csv"
        command = [str(part) for part in self.source_cmd_factory()]
        if not command:
            raise RuntimeError("el comando de captura está vacío")
        log_event(
            "INFO",
            "ingest.spawn",
            channel=self.channel_key,
            generation=generation,
            source=command[0],
            command=[redact(part) for part in command],
        )
        try:
            source = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"No está en PATH: {command[0]}. Instala la herramienta y abre una terminal nueva."
            ) from exc
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_segment_command(
                    pattern,
                    playlist,
                    self.segment_seconds,
                    input_format=self.input_format,
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            await terminate_process(source)
            raise RuntimeError("No está en PATH: ffmpeg.") from exc
        except Exception:
            await terminate_process(source)
            raise

        self._source = source
        self._ffmpeg = ffmpeg
        self.current_pids = [pid for pid in (source.pid, ffmpeg.pid) if pid]
        tail = _Tail()
        pump = asyncio.create_task(pump_stdout(source.stdout, ffmpeg.stdin))
        drain_source = asyncio.create_task(
            drain_lines(source.stderr, lambda line: self._on_stderr("source", line, tail))
        )
        drain_ffmpeg = asyncio.create_task(
            drain_lines(ffmpeg.stderr, lambda line: self._on_stderr("ffmpeg", line, tail))
        )
        watch = asyncio.create_task(self._watch(playlist))
        stop_task = asyncio.create_task(self._stop.wait())
        source_wait = asyncio.create_task(source.wait())
        ffmpeg_wait = asyncio.create_task(ffmpeg.wait())
        try:
            done, _pending = await asyncio.wait(
                {stop_task, source_wait, ffmpeg_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            graceful = stop_task not in done
            if graceful and source_wait in done and ffmpeg_wait not in done:
                try:
                    await asyncio.wait_for(pump, timeout=20)
                except TimeoutError:
                    pass
                else:
                    try:
                        await asyncio.wait_for(ffmpeg_wait, timeout=30)
                    except TimeoutError:
                        pass
        finally:
            await self._terminate_children()
            for task in (pump, watch, stop_task, source_wait, ffmpeg_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                pump,
                drain_source,
                drain_ffmpeg,
                watch,
                stop_task,
                source_wait,
                ffmpeg_wait,
                return_exceptions=True,
            )
            self.current_pids = []
            self._source = None
            self._ffmpeg = None
            await self._ingest_csv(playlist)

        code = ffmpeg.returncode if ffmpeg.returncode is not None else -1
        if code not in (0, 255) and not self._stop.is_set():
            log_event(
                "WARNING",
                "ingest.exit",
                channel=self.channel_key,
                code=code,
                tail=tail.text(),
            )
        return code

    async def _terminate_children(self) -> None:
        async with self._proc_lock:
            source, ffmpeg = self._source, self._ffmpeg
            await terminate_process(source)
            if ffmpeg is not None and ffmpeg.stdin is not None and not ffmpeg.stdin.is_closing():
                try:
                    ffmpeg.stdin.close()
                except (OSError, RuntimeError):
                    pass
            await terminate_process(ffmpeg)

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    def _alloc_generation(self) -> int:
        highest = 0
        if self.directory.exists():
            for path in list(self.directory.glob("g*_*.ts")) + list(self.directory.glob("g*.csv")):
                match = re.match(r"g(\d+)", path.name)
                if match:
                    highest = max(highest, int(match.group(1)))
        self._generation = max(self._generation, highest) + 1
        return self._generation

    async def _watch(self, playlist: Path) -> None:
        try:
            while True:
                await self._ingest_csv(playlist)
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise

    async def _ingest_csv(self, playlist: Path) -> None:
        if not playlist.exists():
            return
        try:
            text = playlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for filename, start, end in parse_segment_csv(text):
            segment = self._add_segment(filename, start, end)
            if segment is not None and self.on_segment is not None:
                await self.on_segment(segment)

    def _add_segment(self, filename: str, start: float, end: float) -> Segment | None:
        if self.index is None:
            return None
        name = Path(filename).name
        if name in self._seen or name in self._ignored:
            return None
        path = self.directory / name
        if not path.exists():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        duration = end - start
        if duration <= 0.05:
            self._ignored.add(name)
            return None
        self._seen.add(name)
        wall_start, wall_end = wall_window(duration, end=utc_now())
        meta = self.session_meta
        segment = Segment(
            file=name,
            duration=duration,
            wall_start=wall_start,
            wall_end=wall_end,
            size_bytes=size,
            session_id=meta.get("session_id"),
            stream_url=meta.get("stream_url"),
            stream_title=meta.get("stream_title"),
            streamer=meta.get("streamer"),
            platform=meta.get("platform") or self.platform,
        )
        self.index.add(segment)
        removed = apply_retention(
            self.index,
            now=utc_now(),
            buffer_minutes=self.buffer_minutes,
        )
        self._seen = {item.file for item in self.index.segments}
        self.index.save()
        log_event(
            "INFO",
            "segment.ready",
            channel=self.channel_key,
            file=name,
            duration=round(duration, 3),
            pruned=len(removed),
        )
        return segment

    def _on_stderr(self, name: str, line: str, tail: _Tail) -> None:
        tail.add(f"{name}: {line}")
        log_event("DEBUG", "ingest.stderr", channel=self.channel_key, source=name, line=line)


class _Tail:
    def __init__(self) -> None:
        self.lines: deque[str] = deque(maxlen=40)

    def add(self, line: str) -> None:
        if line:
            self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)[-2000:]
