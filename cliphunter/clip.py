"""Cut the last N seconds of a channel buffer without re-encoding.

``run`` and ``clip`` do not share memory. ``clip`` reads ``index.json``, which
``run`` replaces atomically, and writes the mp4, a JSON sidecar, and a SQLite row.
Keyframe precision: whole closed segments only. The cut can run long by up to
one segment, and it does not include the segment ffmpeg is still writing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cliphunter.atomic import atomic_write_json
from cliphunter.config import AppConfig, ChannelConfig, channel_key
from cliphunter.index import INDEX_FILENAME, Segment, SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.procutil import run_process
from cliphunter.storage.repo import Repo
from cliphunter.timeutil import utc_iso


class ClipError(Exception):
    """The buffer could not be cut."""


@dataclass
class ClipPlan:
    selected: list[Segment]
    attribution: dict
    concat_lines: list[str]


@dataclass
class ClipResult:
    path: Path
    sidecar: Path
    attribution: dict
    clip_id: int | None
    duration: float


def find_channel(config: AppConfig, canal: str) -> ChannelConfig:
    needle = canal.strip().lstrip("@").lower()
    if not needle:
        raise ClipError("Indica el canal con --canal.")
    exact: list[ChannelConfig] = []
    partial: list[ChannelConfig] = []
    for channel in config.channels:
        key = channel_key(channel).lower()
        handle = (channel.handle or "").lstrip("@").lower()
        display = channel.display_name.lower()
        ident = (channel.id or "").lower()
        if needle in {key, handle, display, ident}:
            exact.append(channel)
        elif needle in key or (handle and needle in handle):
            partial.append(channel)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        names = ", ".join(channel_key(channel) for channel in exact)
        raise ClipError(f"Hay varios canales que coinciden con {canal!r}: {names}")
    if len(partial) == 1:
        return partial[0]
    raise ClipError(
        f"No encuentro el canal {canal!r} en la configuración. "
        "Usa el handle o la clave (por ejemplo youtube_TuCanal)."
    )


def index_path_for(config: AppConfig, channel: ChannelConfig) -> Path:
    return config.buffer_dir / channel_key(channel) / INDEX_FILENAME


def default_clip_path(clips_dir: Path, key: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return clips_dir / f"{key}_{stamp}.mp4"


def plan_from_segments(
    index: SegmentIndex,
    selected: list[Segment],
    *,
    requested_seconds: float | None = None,
) -> ClipPlan:
    if not selected:
        raise ClipError(
            "El buffer no tiene segmentos cerrados para ese canal. "
            "Espera unos segundos a que ffmpeg cierre el primer segmento."
        )
    source_duration = sum(segment.duration for segment in selected)
    if requested_seconds is None:
        requested_seconds = source_duration
    newest = selected[-1]
    oldest = selected[0]
    session_ids = {segment.session_id for segment in selected}
    attribution = {
        "streamer": newest.streamer or index.session.get("streamer"),
        "platform": newest.platform or index.platform,
        "url": newest.stream_url or index.session.get("stream_url"),
        "stream_title": newest.stream_title or index.session.get("stream_title"),
        "channel_context": index.session.get("context") or "",
        "clip_start": oldest.wall_start,
        "clip_end": newest.wall_end,
        "requested_seconds": requested_seconds,
        "actual_seconds": round(source_duration, 3),
        "source_duration_seconds": round(source_duration, 3),
        "precision": "keyframe-segment",
        "precision_note": (
            "Corte sin re-encode sobre segmentos completos. "
            "La duración puede superar lo pedido hasta un segmento "
            "(ffmpeg corta en el keyframe más cercano, no en el frame exacto). "
            "El segmento que todavía se está escribiendo no entra."
        ),
        "short_buffer": source_duration + 1e-3 < requested_seconds,
        "spans_multiple_sessions": len(session_ids) > 1,
        "session_id": newest.session_id,
        "source_segments": [segment.file for segment in selected],
        "created_at": utc_iso(),
    }
    authorization = index.session.get("authorization")
    if authorization:
        attribution["authorization"] = authorization
    lines: list[str] = []
    for segment in selected:
        path = (index.directory / segment.file).resolve()
        if not path.exists():
            raise ClipError(f"Falta el segmento {segment.file} en el buffer. Prueba de nuevo.")
        escaped = path.as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return ClipPlan(selected=selected, attribution=attribution, concat_lines=lines)


def plan_clip(index: SegmentIndex, seconds: float) -> ClipPlan:
    if seconds <= 0:
        raise ClipError("--ultimos tiene que ser mayor que 0.")
    return plan_from_segments(index, index.select_last(seconds), requested_seconds=seconds)


def _mux_command(concat: Path, output: Path, *, bitstream_filter: bool) -> list[str]:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat.as_posix(),
        "-c",
        "copy",
    ]
    if bitstream_filter:
        command.extend(["-bsf:a", "aac_adtstoasc"])
    command.extend(["-movflags", "+faststart", output.as_posix()])
    return command


def probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
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
    )
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


async def mux_plan(plan: ClipPlan, output: Path) -> float:
    if shutil.which("ffmpeg") is None:
        raise ClipError("ffmpeg no está en PATH.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cliphunter-") as temporary:
        concat = Path(temporary) / "concat.txt"
        concat.write_text("\n".join(plan.concat_lines) + "\n", encoding="utf-8")
        first_error = ""
        for use_filter in (True, False):
            if output.exists():
                output.unlink()
            code, error = await run_process(_mux_command(concat, output, bitstream_filter=use_filter))
            if code == 0 and output.exists() and output.stat().st_size > 0:
                probed = probe_duration(output)
                if probed and probed > 0:
                    plan.attribution["actual_seconds"] = round(probed, 3)
                    return probed
                return float(plan.attribution["source_duration_seconds"])
            first_error = first_error or error
            log_event(
                "WARNING",
                "clip.mux_retry",
                bitstream_filter=use_filter,
                code=code,
            )
        if output.exists():
            output.unlink()
        tail = (first_error or "ffmpeg no escribió el mp4").strip()[-1500:]
        raise ClipError(f"ffmpeg no pudo montar el clip sin re-encode:\n{tail}")


async def create_clip(
    *,
    index_path: Path,
    seconds: float,
    output: Path,
    repo: Repo | None = None,
    channel_id: int | None = None,
    authorization: dict | None = None,
    weight: float | None = None,
    credit: str | None = None,
    status: str = "ready",
    breakdown: dict | None = None,
) -> ClipResult:
    if not index_path.exists():
        raise ClipError(
            f"No hay índice en {index_path}. ¿El canal está en directo y `run` lleva unos segundos grabando?"
        )
    index = SegmentIndex.load(index_path)
    plan = plan_clip(index, seconds)
    plan.attribution["buffer_index"] = str(index_path.resolve())
    if authorization:
        plan.attribution["authorization"] = authorization
    if credit:
        plan.attribution["credit"] = credit
    if breakdown is not None:
        plan.attribution["breakdown"] = breakdown
    if weight is not None:
        plan.attribution["calibration_weight"] = weight
    duration = await mux_plan(plan, output)
    plan.attribution["file_path"] = str(output.resolve())
    sidecar = output.with_suffix(".json")
    atomic_write_json(sidecar, plan.attribution)
    clip_id: int | None = None
    if repo is not None and channel_id is not None:
        session_id = plan.attribution.get("session_id")
        if isinstance(session_id, int) and not await repo.session_exists(session_id):
            session_id = None
        elif not isinstance(session_id, int):
            session_id = None
        clip_id = await repo.insert_clip(
            channel_id=channel_id,
            session_id=session_id,
            kind="manual",
            status=status,
            attribution=plan.attribution,
            file_path=str(output.resolve()),
            duration_seconds=duration,
            weight=weight,
            breakdown=breakdown,
        )
        plan.attribution["clip_id"] = clip_id
        atomic_write_json(sidecar, plan.attribution)
    log_event(
        "INFO",
        "clip.created",
        path=str(output),
        sidecar=str(sidecar),
        clip_id=clip_id,
        requested_seconds=seconds,
        actual_seconds=plan.attribution.get("actual_seconds"),
        streamer=plan.attribution.get("streamer"),
        platform=plan.attribution.get("platform"),
    )
    return ClipResult(
        path=output,
        sidecar=sidecar,
        attribution=plan.attribution,
        clip_id=clip_id,
        duration=duration,
    )
