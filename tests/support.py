"""ffmpeg fixtures for tests. Skips when ffmpeg is not installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg is not installed")
    return path


def require_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        pytest.skip("ffprobe is not installed")
    return path


def generate_ts(path: Path, seconds: int = 12) -> Path:
    ffmpeg = require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000",
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "10",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-f",
            "mpegts",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return path


def probe_duration(path: Path) -> float:
    ffprobe = require_ffprobe()
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())
