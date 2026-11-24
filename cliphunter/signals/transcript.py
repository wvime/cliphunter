"""Optional transcript. Never blocks the cut.

Audio is decoded with ffmpeg to 16 kHz mono PCM and passed to
faster-whisper as a float array. Giving the library a media path goes
through PyAV, which fails on the owner's Windows install
(``open() got an unexpected keyword argument 'metadata_errors'``).
"""

from __future__ import annotations

from pathlib import Path

from cliphunter.clip import probe_duration
from cliphunter.editorial.audio import read_pcm, s16le_to_floats, transcribe_samples, whisper_available
from cliphunter.jsonlog import log_event

_warned = False


async def transcribe_clip(
    *,
    media: Path,
    model_name: str = "base",
    seconds: float = 60.0,
) -> str | None:
    global _warned
    if not whisper_available():
        if not _warned:
            _warned = True
            log_event(
                "WARNING",
                "transcript.unavailable",
                message="faster-whisper no está instalado; el clip se corta igual",
            )
        return None
    try:
        probed = probe_duration(media)
        start = None
        if probed and probed > seconds:
            start = probed - seconds
        pcm = await read_pcm(media, start=start, duration=seconds)
        import asyncio

        pieces = await asyncio.to_thread(
            transcribe_samples,
            s16le_to_floats(pcm),
            model_name=model_name,
            language="es",
        )
        text = " ".join(piece[2] for piece in pieces).strip()
        return text or None
    except Exception as exc:
        log_event("WARNING", "transcript.failed", error=str(exc))
        return None
