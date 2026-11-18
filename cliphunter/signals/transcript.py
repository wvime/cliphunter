"""Optional transcript of the last 60 s. Never blocks the cut.

faster-whisper is imported only when a moment fires. CPU int8. If the
package is missing, clipping continues and the log says so once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from cliphunter.jsonlog import log_event

_warned = False
_model = None
_model_name = ""


async def transcribe_clip(
    *,
    media: Path,
    model_name: str = "base",
    seconds: float = 60.0,
) -> str | None:
    global _warned, _model, _model_name
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        if not _warned:
            _warned = True
            log_event(
                "WARNING",
                "transcript.unavailable",
                message="faster-whisper no está instalado; el clip se corta igual",
            )
        return None
    wav = media.with_suffix(".whisper.wav")
    try:
        await _extract(media, wav, seconds)
        if _model is None or _model_name != model_name:
            _model = await asyncio.to_thread(
                WhisperModel, model_name, "cpu", compute_type="int8"
            )
            _model_name = model_name
        segments, _info = await asyncio.to_thread(
            lambda: _model.transcribe(str(wav), vad_filter=True)
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text or None
    except Exception as exc:
        log_event("WARNING", "transcript.failed", error=str(exc))
        return None
    finally:
        wav.unlink(missing_ok=True)


async def _extract(media: Path, wav: Path, seconds: float) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-sseof",
        f"-{seconds}",
        "-i",
        str(media),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-y",
        str(wav),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
