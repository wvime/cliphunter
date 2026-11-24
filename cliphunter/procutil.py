"""Subprocess helpers that stay on asyncio and work on Windows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def terminate_process(
    proc: asyncio.subprocess.Process | None,
    *,
    timeout: float = 5.0,
) -> None:
    """Stop a child with terminate(), then kill() if it ignores it.

    ``terminate()`` is SIGTERM on POSIX and TerminateProcess on Windows.
    """

    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except OSError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        return
    except TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except OSError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        pass


async def pump_stdout(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Copy streamlink's stdout into ffmpeg's stdin. Bytes only, no decode."""

    if reader is None or writer is None:
        return
    try:
        while True:
            chunk = await reader.read(256 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
        return
    finally:
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            pass


async def drain_lines(
    stream: asyncio.StreamReader | None,
    on_line: Callable[[str], None],
) -> None:
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            on_line(line.decode("utf-8", errors="replace").rstrip())
    except asyncio.CancelledError:
        raise
    except (OSError, RuntimeError):
        return


async def run_process(cmd: list[str], *, timeout: float = 180.0) -> tuple[int, str]:
    """Run a finite command. On timeout the process is terminated."""

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await terminate_process(proc)
        raise
    text = (stderr or b"").decode("utf-8", errors="replace")
    return proc.returncode or 0, text
