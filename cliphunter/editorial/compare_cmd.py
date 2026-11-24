"""``python -m cliphunter compare archivo.mp4 --chat chat.json``."""

from __future__ import annotations

import sys
from pathlib import Path

from cliphunter.config import load_config
from cliphunter.editorial.bounds import Line
from cliphunter.editorial.compare import (
    Pick,
    energy_picks,
    live_sim_windows,
    old_picks_from_moments,
    picks_from_directory,
    render_report,
    render_table,
)
from cliphunter.editorial.cues import find_cues
from cliphunter.env import load_env
from cliphunter.replay import load_chat, replay_moments
from cliphunter.signals.aggregate import channel_threshold


def compare_command(args) -> int:
    video = getattr(args, "archivo", None)
    if not video:
        print("compare necesita un vídeo y, si lo tienes, --chat chat.json", file=sys.stderr)
        return 2
    config = load_config(Path(args.config))
    messages = load_chat(Path(args.chat)) if getattr(args, "chat", None) else []
    duration = _duration(Path(video), messages)
    channel = config.channels[0] if config.channels else None
    old_dir = getattr(args, "old_clips", None)
    if old_dir:
        from cliphunter.clip import probe_duration

        old = picks_from_directory(Path(old_dir), probe=probe_duration)
        moments = []
    else:
        moments = (
            replay_moments(
                messages,
                duration=duration,
                weights=channel.weights if channel else None,
                emotes=list(channel.emotes_hype) if channel else None,
                threshold=channel_threshold(channel.thresholds if channel else None),
                hysteresis=config.hysteresis,
                window=config.signal_window_seconds,
                step=config.signal_interval_seconds,
                min_seconds=config.clip_min_seconds,
                max_seconds=config.clip_max_seconds,
                target_seconds=config.clip_target_seconds,
                pre_roll=config.clip_pre_roll_seconds,
            )
            if messages
            else []
        )
        old = old_picks_from_moments(moments)
        if not old:
            old = _audio_old_picks(Path(video))
    lines = [Line(message.t, message.t + 1.5, message.text) for message in messages]
    spoken = _spoken_lines(Path(video), model=config.whisper_model)
    if spoken:
        lines.extend(spoken)
    env = load_env()
    if env.anthropic_api_key:
        accepted, rejected = _with_judge(config, env.anthropic_api_key, lines, old)
        note = "El selector nuevo llamó a Claude."
    else:
        accepted, rejected = _without_judge(lines, old)
        note = (
            "Sin ANTHROPIC_API_KEY el selector nuevo no inventa título, categoría ni puntuación. "
            "Marca candidatos por pista de chat o de transcripción y rechaza los picos que solo fueron señal."
        )
        if not spoken:
            note += " No hay transcripción de audio en este reporte (falta faster-whisper o el archivo no se pudo leer)."
    judged = list(accepted) + list(rejected)
    if getattr(args, "live_sim", False):
        judged = _live_sim_candidates(config, env.anthropic_api_key, lines, duration) or judged
    people = _load_people(config)
    if people:
        note += "\n\nFicha: " + ", ".join(person["name"] for person in people)
    report = (
        note
        + "\n\n## Candidatos juzgados\n\n"
        + render_table(judged)
        + "\n\n"
        + render_report(old, [item for item in judged if item.accepted], rejected=[item for item in judged if not item.accepted])
    )
    out = getattr(args, "out", None)
    if out:
        Path(out).write_text(report, encoding="utf-8")
        print(f"compare: {out}")
    else:
        print(report)
    return 0


def _audio_old_picks(video: Path) -> list[Pick]:
    from cliphunter.editorial.audio import read_pcm, s16le_to_floats

    if not video.exists():
        return []
    import asyncio

    async def _go() -> list[float]:
        pcm = await read_pcm(video, duration=480)
        return s16le_to_floats(pcm)

    try:
        samples = asyncio.run(_go())
    except Exception:
        return []
    return energy_picks(samples)


def _live_sim_candidates(config, api_key: str | None, lines: list[Line], duration: float) -> list[Pick]:
    windows = live_sim_windows(
        duration,
        sweep=config.editorial_sweep_seconds,
        before=config.editorial_before_seconds,
        after=config.editorial_after_seconds,
        cooldown=45,
    )
    picks: list[Pick] = []
    if not api_key:
        for start, end, _mark in windows:
            window_lines = [line for line in lines if line.end >= start and line.start <= end]
            cues = find_cues(window_lines, [])
            picks.append(
                Pick(
                    start=start,
                    end=end,
                    trigger="live-sim",
                    why="sin ANTHROPIC_API_KEY" if not cues else "pista " + ",".join(cues) + "; sin clave",
                    accepted=False,
                )
            )
        return picks
    from cliphunter.editorial.judge import ClaudeJudge

    judge = ClaudeJudge(api_key, model=config.editorial_model, minimum=config.editorial_min_score, resample=False)

    async def _run() -> list[Pick]:
        found: list[Pick] = []
        for start, end, _mark in windows[:8]:
            window_lines = [line for line in lines if line.end >= start and line.start <= end]
            t0 = window_lines[0].start if window_lines else start
            verdict = await judge.judge(
                {
                    "t0": t0,
                    "transcript": [
                        {"start": round(line.start - t0, 2), "end": round(line.end - t0, 2), "text": line.text}
                        for line in window_lines
                    ],
                    "cues": ["live-sim"],
                    "context": "Sin ficha en este paquete si no se cargó la base.",
                }
            )
            judge_start = start if verdict.setup_start is None else t0 + verdict.setup_start
            judge_end = end if verdict.end is None else t0 + verdict.end
            found.append(
                Pick(
                    start=judge_start,
                    end=judge_end,
                    score=verdict.total,
                    trigger="live-sim",
                    category=verdict.category,
                    title=verdict.title,
                    tier=verdict.tier,
                    why=verdict.why,
                    participants=", ".join(verdict.participants),
                    accepted=bool(verdict.keep),
                )
            )
        return found

    import asyncio

    return asyncio.run(_run())


def _load_people(config) -> list[dict]:
    import asyncio

    from cliphunter.storage.repo import SqliteRepo

    repo = SqliteRepo(config.resolved_database_path())

    async def _go() -> list[dict]:
        await repo.init()
        try:
            return await repo.list_participants()
        finally:
            await repo.close()

    try:
        return asyncio.run(_go())
    except Exception:
        return []


def _without_judge(lines: list[Line], old: list[Pick]) -> tuple[list[Pick], list[Pick]]:
    accepted: list[Pick] = []
    for start, end, cues in _clusters(lines):
        accepted.append(
            Pick(
                start=start,
                end=end,
                trigger="pista:" + ",".join(cues),
                why="candidato por chat o transcripción; falta Claude para el veredicto",
                accepted=True,
            )
        )
    rejected: list[Pick] = []
    for pick in old:
        if any(pick.overlaps(item) for item in accepted):
            continue
        rejected.append(
            Pick(
                start=pick.start,
                end=pick.end,
                trigger=pick.trigger,
                tier="rejected",
                why="solo hubo pico de señal; no hay pista de risa, nombre o quiebre",
                accepted=False,
            )
        )
    return accepted, rejected


def _clusters(lines: list[Line]) -> list[tuple[float, float, list[str]]]:
    hits = []
    for line in lines:
        cues = find_cues([line], [])
        if cues:
            hits.append((line, cues))
    if not hits:
        return []
    groups: list[tuple[float, float, list[str]]] = []
    start = hits[0][0].start
    end = hits[0][0].end
    cues: list[str] = list(hits[0][1])
    for line, found in hits[1:]:
        if line.start - end <= 8:
            end = max(end, line.end)
            for cue in found:
                if cue not in cues:
                    cues.append(cue)
            continue
        groups.append((start, max(end, start + 15), cues))
        start, end, cues = line.start, line.end, list(found)
    groups.append((start, max(end, start + 15), cues))
    return groups


def _with_judge(config, api_key: str, lines: list[Line], old: list[Pick]) -> tuple[list[Pick], list[Pick]]:
    """One cheap call per cue cluster and per old pick, capped so a VOD cannot fan out."""

    import asyncio

    from cliphunter.editorial.judge import ClaudeJudge

    judge = ClaudeJudge(api_key, model=config.editorial_model, minimum=config.editorial_min_score)
    windows = _clusters(lines)[:8]
    packets = []
    for start, end, cues in windows:
        packets.append((start, end, cues, _packet(lines, start, end, cues)))
    for pick in old[:4]:
        packets.append((pick.start, pick.end, ["señal"], _packet(lines, pick.start, pick.end, ["señal"])))

    async def _run():
        results = []
        for start, end, cues, packet in packets:
            verdict = await judge.judge(packet)
            results.append((start, end, cues, verdict))
        return results

    judged = asyncio.run(_run())
    accepted: list[Pick] = []
    rejected: list[Pick] = []
    for start, end, cues, verdict in judged:
        pick = Pick(
            start=start,
            end=end,
            score=verdict.total,
            trigger=",".join(cues),
            category=verdict.category,
            title=verdict.title,
            tier=verdict.tier,
            why=verdict.why or verdict.storyline_note,
            accepted=bool(verdict.keep),
        )
        (accepted if pick.accepted else rejected).append(pick)
    return accepted, rejected


def _packet(lines: list[Line], start: float, end: float, cues: list[str]) -> dict:
    window = [line for line in lines if line.end >= start - 5 and line.start <= end + 5]
    t0 = window[0].start if window else start
    return {
        "t0": t0,
        "transcript": [
            {"start": round(line.start - t0, 2), "end": round(line.end - t0, 2), "text": line.text}
            for line in window
        ],
        "cues": cues,
        "context": "Sin ficha en este reporte. No inventes relaciones.",
    }


def _spoken_lines(video: Path, *, model: str) -> list[Line]:
    """First 8 minutes only, so a long VOD cannot pin the CPU overnight."""

    from cliphunter.editorial.audio import read_pcm, s16le_to_floats, transcribe_samples, whisper_available

    if not whisper_available() or not video.exists():
        return []
    import asyncio

    async def _go() -> list[tuple[float, float, str]]:
        pcm = await read_pcm(video, duration=480)
        return await asyncio.to_thread(
            transcribe_samples,
            s16le_to_floats(pcm),
            model_name=model,
            language="es",
        )

    try:
        pieces = asyncio.run(_go())
    except Exception as exc:
        print(f"compare: sin transcripción ({exc})", file=sys.stderr)
        return []
    return [Line(piece[0], piece[1], piece[2]) for piece in pieces]


def _duration(video: Path, messages) -> float:
    import shutil
    import subprocess

    if shutil.which("ffprobe"):
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
        )
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    if messages:
        return messages[-1].t + 1
    return 0.0
