"""Decode with ffmpeg and hand float32 PCM to faster-whisper.

faster-whisper's bundled PyAV fails on this Windows install with
``open() got an unexpected keyword argument 'metadata_errors'``. Passing a
file path hits that decoder. A 16 kHz mono PCM buffer does not.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

from cliphunter.jsonlog import log_event

_model = None
_model_name = ""
_warned = False


def s16le_to_floats(data: bytes) -> list[float]:
    count = len(data) // 2
    if count == 0:
        return []
    samples = struct.unpack("<" + "h" * count, data[: count * 2])
    return [sample / 32768.0 for sample in samples]


def as_numpy(samples: list[float]):
    import numpy as np

    return np.asarray(samples, dtype="float32")


async def read_pcm(path: Path, *, start: float | None = None, duration: float | None = None) -> bytes:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
    ]
    if start is not None and start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", str(path)])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(["-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("ffmpeg pcm tardó demasiado") from exc
    if process.returncode not in (0, None) and not stdout:
        raise RuntimeError(f"ffmpeg pcm {process.returncode}")
    return stdout or b""


_whisper_ok: bool | None = None


def whisper_available() -> bool:
    global _whisper_ok
    if _whisper_ok is None:
        try:
            import faster_whisper  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            _whisper_ok = False
        else:
            _whisper_ok = True
    return _whisper_ok


def load_model(model_name: str):
    global _model, _model_name, _warned
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        if not _warned:
            _warned = True
            log_event(
                "WARNING",
                "transcript.unavailable",
                message="faster-whisper no está instalado; se sigue sin transcripción",
            )
        return None
    if _model is None or _model_name != model_name:
        _model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=1)
        _model_name = model_name
    return _model


def transcribe_samples(
    samples: list[float],
    *,
    model_name: str,
    language: str = "es",
    names: list[str] | None = None,
) -> list[tuple[float, float, str, tuple]]:
    """Return ``(start, end, text, words)`` in seconds relative to this PCM.

    ``names`` is the participant list. It is passed as ``initial_prompt`` and,
    when this faster-whisper build accepts it, as ``hotwords``. ``small`` is
    about three times the CPU of ``base``.
    """

    model = load_model(model_name)
    if model is None or not samples:
        return []
    from cliphunter.editorial.names import correct_tokens

    audio = as_numpy(samples)
    prompt = None
    hotwords = None
    if names:
        prompt = "Nombres: " + ", ".join(names[:40])
        hotwords = " ".join(names[:30])
    kwargs = {
        "language": language or None,
        "vad_filter": True,
        "beam_size": 1,
        "best_of": 1,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "initial_prompt": prompt,
    }
    try:
        segments, _info = model.transcribe(audio, hotwords=hotwords, **kwargs)
    except TypeError:
        segments, _info = model.transcribe(audio, **kwargs)
    lines: list[tuple[float, float, str, tuple]] = []
    for segment in segments:
        text = correct_tokens((segment.text or "").strip(), names or [])
        if not text:
            continue
        words = []
        for word in getattr(segment, "words", None) or []:
            token = correct_tokens((getattr(word, "word", "") or "").strip(), names or [])
            if token:
                words.append((float(word.start), float(word.end), token))
        lines.append((float(segment.start), float(segment.end), text, tuple(words)))
    return lines
