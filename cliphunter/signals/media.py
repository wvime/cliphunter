"""Cheap audio and scene reads. One thread, low rate, cached per segment."""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

from cliphunter.signals.base import SignalUnavailable

_SCENE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def rms_from_s16le(data: bytes) -> float:
    count = len(data) // 2
    if count == 0:
        raise SignalUnavailable("audio vacío")
    samples = struct.unpack("<" + "h" * count, data[: count * 2])
    mean_square = sum(sample * sample for sample in samples) / count
    return math.sqrt(mean_square) / 32768.0


def scene_scores_from_text(text: str) -> float:
    scores = [float(match) for match in _SCENE.findall(text)]
    if not scores:
        return 0.0
    return max(scores)


async def audio_rms(path: Path) -> float:
    return rms_from_s16le(await _pcm(path))


async def _pcm(path: Path) -> bytes:
    data, _err, code = await _ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        timeout=8,
    )
    if code not in (0, None) and not data:
        raise SignalUnavailable(f"ffmpeg audio {code}")
    return data or b""


async def scene_score(path: Path) -> float:
    stdout, stderr, _code = await _ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-threads",
            "1",
            "-i",
            str(path),
            "-vf",
            "scale=160:90,fps=2,select='gte(scene,0)',metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=8,
    )
    text = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    return scene_scores_from_text(text)


async def _ffmpeg(args: list[str], *, timeout: float) -> tuple[bytes, bytes, int | None]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SignalUnavailable("ffmpeg tardó demasiado") from exc
    return stdout or b"", stderr or b"", process.returncode
