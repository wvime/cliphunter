"""Replay a VOD plus a chat dump through the same scorer as live.

``python -m cliphunter replay archivo.mp4 --chat chat.json``

Chat JSON is a list of objects with a time (``t``, ``ts`` or ``offset``)
and text (``text`` or ``message``). The run is offline and faster than
realtime. ``--cut`` writes copy-only mp4 slices when ffmpeg is available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from cliphunter.config import load_config
from cliphunter.signals.aggregate import (
    MomentTracker,
    canonical_weights,
    channel_threshold,
    choose_bounds,
    combine,
)
from cliphunter.signals.base import ChatMessage, SignalWindow
from cliphunter.signals.baseline import RollingBaseline
from cliphunter.signals.features import AudioEnergy, ChatVelocity, EmoteBurst, SceneChange, ViewerDelta


def load_chat(path: Path) -> list[ChatMessage]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("messages") or payload.get("chat") or []
    messages: list[ChatMessage] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        stamp = item.get("t", item.get("ts", item.get("offset", item.get("time"))))
        text = item.get("text", item.get("message", ""))
        if stamp is None:
            continue
        messages.append(ChatMessage(text=str(text), t=float(stamp)))
    messages.sort(key=lambda message: message.t)
    return messages


def replay_moments(
    messages: list[ChatMessage],
    *,
    duration: float,
    weights: dict[str, float] | None = None,
    emotes: list[str] | None = None,
    threshold: float = 2.5,
    hysteresis: float = 0.65,
    window: float = 10,
    step: float = 2,
    min_seconds: float = 15,
    max_seconds: float = 45,
    target_seconds: float = 30,
    pre_roll: float = 8,
    min_samples: int = 5,
) -> list[dict]:
    signals = [ChatVelocity(), EmoteBurst(emotes), AudioEnergy(), SceneChange(), ViewerDelta()]
    weights = canonical_weights(weights)
    baselines = {signal.name: RollingBaseline(horizon=600, min_samples=min_samples) for signal in signals}
    tracker = MomentTracker()
    found: list[dict] = []
    peak_tick = None
    t = 0.0
    while t <= duration:
        window_messages = [item for item in messages if t - window <= item.t <= t]
        sample = SignalWindow(
            channel_key="replay",
            start=t - window,
            end=t,
            messages=window_messages,
            audio_rms=None,
            scene_score=None,
            viewer_delta=None,
        )
        parts: dict[str, dict[str, float]] = {}
        degraded: list[str] = []
        for signal in signals:
            try:
                raw = float(signal.compute(sample))
            except Exception:
                degraded.append(signal.name)
                continue
            parts[signal.name] = {
                "raw": raw,
                "z": round(baselines[signal.name].add(t, raw), 3),
                "weight": float(weights.get(signal.name, 0)),
            }
        tick = combine(parts, degraded)
        phase = tracker.push(t, tick.score, threshold=threshold, release=threshold * hysteresis, max_len=max_seconds)
        if phase == "rising" and (peak_tick is None or tick.score >= peak_tick.score):
            peak_tick = tick
        if phase == "fire":
            start, end, peak = choose_bounds(
                tracker.samples,
                min_seconds=min_seconds,
                max_seconds=max_seconds,
                target_seconds=target_seconds,
                pre_roll=pre_roll,
            )
            saved = peak_tick or tick
            found.append(
                {
                    "start": max(0.0, start),
                    "end": min(duration, end),
                    "peak": peak,
                    "score": round(saved.score, 3),
                    "breakdown": {"score": round(saved.score, 3), "signals": saved.parts, "degraded": saved.degraded},
                }
            )
            peak_tick = None
            tracker.reset()
        t += step
    return found


def replay_command(args) -> int:
    video = getattr(args, "archivo", None)
    if not video:
        print("replay necesita un archivo.mp4 y --chat chat.json", file=__import__("sys").stderr)
        return 2
    chat = getattr(args, "chat", None)
    if not chat:
        print("replay necesita --chat chat.json", file=__import__("sys").stderr)
        return 2
    config = load_config(Path(args.config))
    messages = load_chat(Path(chat))
    duration = _duration(Path(video), messages)
    channel = config.channels[0] if config.channels else None
    moments = replay_moments(
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
    if not moments:
        print("replay: ningún momento superó el umbral.")
        return 0
    out_dir = config.resolved_clips_dir()
    for index, moment in enumerate(moments, start=1):
        print(
            f"momento {index}: {moment['start']:.1f}s–{moment['end']:.1f}s "
            f"pico {moment['peak']:.1f}s score {moment['score']}"
        )
        if getattr(args, "cut", False) and shutil.which("ffmpeg"):
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"replay_{index:02d}.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{max(0.0, moment['start']):.3f}",
                    "-to",
                    f"{moment['end']:.3f}",
                    "-i",
                    str(video),
                    "-c",
                    "copy",
                    str(target),
                ],
                check=False,
            )
            print(f"  clip {target}")
    return 0


def _duration(video: Path, messages: list[ChatMessage]) -> float:
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
