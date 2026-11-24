"""Re-encode a transcript window. This path does not snap to keyframes."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cliphunter.clip import plan_from_segments, probe_duration
from cliphunter.index import SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.procutil import run_process
from cliphunter.signals.aggregate import segments_for_window
from cliphunter.timeutil import utc_iso


def precise_ffmpeg_command(source: Path, output: Path, *, start: float, duration: float) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        str(source),
        "-t",
        f"{max(0.1, duration):.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-threads",
        "1",
        str(output),
    ]


async def cut_precise(
    *,
    index_path: Path,
    start: float,
    end: float,
    output: Path,
) -> tuple[list, dict, float]:
    """Trim ``[start, end]`` (unix seconds) out of the buffer and re-encode it."""

    index = SegmentIndex.load(index_path)
    selected = segments_for_window(
        index,
        start,
        end,
        (start + end) / 2,
        min_seconds=0.5,
        max_seconds=max(1.0, end - start + 30),
    )
    if not selected:
        raise RuntimeError("no hay segmentos para ese intervalo")
    plan = plan_from_segments(index, selected, requested_seconds=max(0.1, end - start))
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg no está en PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cliphunter-precise-") as temporary:
        concat = Path(temporary) / "joined.mp4"
        concat_list = Path(temporary) / "list.txt"
        concat_list.write_text("\n".join(plan.concat_lines) + "\n", encoding="utf-8")
        error = ""
        joined = False
        for bitstream_filter in (True, False):
            if concat.exists():
                concat.unlink()
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list.as_posix(),
                "-c",
                "copy",
            ]
            if bitstream_filter:
                command.extend(["-bsf:a", "aac_adtstoasc"])
            command.append(concat.as_posix())
            code, error = await run_process(command)
            if code == 0 and concat.exists() and concat.stat().st_size > 0:
                joined = True
                break
        if not joined:
            raise RuntimeError((error or "no se pudo unir el buffer")[-800:])
        from cliphunter.timeutil import parse_iso

        origin = parse_iso(selected[0].wall_start).timestamp()
        offset = max(0.0, start - origin)
        duration = max(0.2, end - start)
        code, error = await run_process(precise_ffmpeg_command(concat, output, start=offset, duration=duration))
        if code != 0 or not output.exists() or output.stat().st_size == 0:
            raise RuntimeError((error or "el recorte preciso falló")[-800:])
    probed = probe_duration(output) or duration
    plan.attribution["precision"] = "reencode"
    plan.attribution["precision_note"] = (
        "Recorte reencodado (libx264 veryfast, aac) entre las frases de la transcripción, "
        "no en el keyframe del segmento."
    )
    plan.attribution["clip_start"] = utc_iso_from_unix(start)
    plan.attribution["clip_end"] = utc_iso_from_unix(end)
    plan.attribution["actual_seconds"] = round(probed, 3)
    plan.attribution["requested_seconds"] = round(end - start, 3)
    log_event("INFO", "editorial.cut", path=str(output), seconds=round(probed, 3))
    return selected, plan.attribution, probed


def utc_iso_from_unix(stamp: float) -> str:
    from datetime import datetime, timezone

    return utc_iso(datetime.fromtimestamp(stamp, timezone.utc))
