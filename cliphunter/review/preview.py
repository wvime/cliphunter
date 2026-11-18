"""Shrink a preview only when Telegram would reject the file."""

from __future__ import annotations

from pathlib import Path

TELEGRAM_VIDEO_LIMIT = 50 * 1024 * 1024


def needs_compress(path: Path, *, limit: int = TELEGRAM_VIDEO_LIMIT, size: int | None = None) -> bool:
    actual = path.stat().st_size if size is None else size
    return actual > limit


def compress_preview_command(source: Path, dest: Path) -> list[str]:
    """One-thread 480p transcode. Used only above the 50 MB Telegram limit."""

    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-i",
        source.as_posix(),
        "-vf",
        "scale=-2:480",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        dest.as_posix(),
    ]


async def preview_file(path: Path) -> Path:
    if not needs_compress(path):
        return path
    dest = path.with_name(f"{path.stem}.telegram.mp4")
    if dest.exists() and dest.stat().st_size <= TELEGRAM_VIDEO_LIMIT:
        return dest
    from cliphunter.procutil import run_process

    code, error = await run_process(compress_preview_command(path, dest), timeout=180)
    if code != 0 or not dest.exists():
        raise RuntimeError((error or "ffmpeg no pudo comprimir la vista previa")[-500:])
    return dest
