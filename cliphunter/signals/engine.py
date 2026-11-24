"""Per-stream automatic clipper. A failed signal never stops ingest."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cliphunter.bus import MOMENT_CANDIDATE, Event, EventBus
from cliphunter.clip import create_clip
from cliphunter.config import AppConfig, ChannelConfig
from cliphunter.index import SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.signals.aggregate import (
    MomentTracker,
    ScoreTick,
    canonical_weights,
    channel_cooldown,
    channel_threshold,
    choose_bounds,
    combine,
    segments_for_window,
)
from cliphunter.signals.base import ChatMessage, Signal, SignalWindow
from cliphunter.signals.baseline import RollingBaseline
from cliphunter.signals.features import AudioEnergy, ChatVelocity, EmoteBurst, SceneChange, ViewerDelta
from cliphunter.signals.media import audio_rms, scene_score
from cliphunter.signals.nudge import ChannelNudge, calibration_path, load_nudges, save_nudge
from cliphunter.storage.repo import Repo


@dataclass
class LiveView:
    channel_key: str
    channel_db_id: int
    index_path: Path
    video_id: str | None
    config_id: str | None
    emotes: list[str]
    weights: dict[str, float]
    thresholds: dict[str, float]
    credit: str | None
    authorization: dict | None


@dataclass
class _ChannelRuntime:
    view: LiveView
    tracker: MomentTracker = field(default_factory=MomentTracker)
    baselines: dict[str, RollingBaseline] = field(default_factory=dict)
    messages: list[ChatMessage] = field(default_factory=list)
    audio_cache: dict[str, float] = field(default_factory=dict)
    scene_cache: dict[str, float] = field(default_factory=dict)
    viewers: list[tuple[float, int]] = field(default_factory=list)
    cooldown_until: float = 0.0
    nudge: ChannelNudge | None = None
    seen_decisions: int = 0
    last_chat_poll: float = 0.0
    last_viewer_poll: float = 0.0
    chat_client: object | None = None
    degraded_logged: set[str] = field(default_factory=set)
    peak_tick: ScoreTick | None = None


class AutoClipEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        repo: Repo,
        bus: EventBus,
        channels: list[ChannelConfig],
        min_baseline_samples: int = 5,
        on_moment: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.bus = bus
        self.channels = {_key(channel): channel for channel in channels}
        self.min_baseline_samples = min_baseline_samples
        self.on_moment = on_moment
        self.editorial = None
        self._states: dict[str, _ChannelRuntime] = {}

    def ensure(self, view: LiveView) -> _ChannelRuntime:
        state = self._states.get(view.channel_key)
        if state is None:
            initial = channel_threshold(view.thresholds)
            saved = load_nudges(calibration_path(self.config.resolved_database_path())).get(view.channel_key) or {}
            threshold = float(saved.get("threshold", initial))
            state = _ChannelRuntime(
                view=view,
                nudge=ChannelNudge(
                    threshold=threshold,
                    floor=float(saved.get("floor", round(initial * 0.5, 3))),
                    ceiling=float(saved.get("ceiling", round(initial * 2.0, 3))),
                    consecutive_rejections=int(saved.get("consecutive_rejections", 0)),
                    consecutive_approvals=int(saved.get("consecutive_approvals", 0)),
                    weights=dict(saved.get("weights") or {}),
                ),
            )
            self._states[view.channel_key] = state
        else:
            state.view = view
        return state

    def note_chat(self, channel_key: str, messages: list[ChatMessage]) -> None:
        state = self._states.get(channel_key)
        if state is None:
            return
        known = {(item.t, item.text) for item in state.messages}
        for message in messages:
            if (message.t, message.text) not in known:
                state.messages.append(message)
        cutoff = (state.messages[-1].t if state.messages else 0) - self.config.baseline_minutes * 60
        state.messages = [item for item in state.messages if item.t >= cutoff]

    def note_viewers(self, channel_key: str, t: float, count: int) -> None:
        state = self._states.get(channel_key)
        if state is None:
            return
        state.viewers.append((t, count))
        state.viewers = state.viewers[-4:]

    async def tick(self, view: LiveView, now: float) -> ScoreTick | None:
        state = self.ensure(view)
        await self._poll_live(state, now)
        if self.editorial is not None:
            try:
                await self.editorial.poll(state, now)
            except Exception as exc:
                log_event("ERROR", "editorial.poll_failed", channel=view.channel_key, error=str(exc))
        if now < state.cooldown_until:
            return None
        window = await self._window(state, now)
        signals = self._signals(view)
        raw_weights = view.weights
        if state.nudge is not None and state.nudge.weights:
            raw_weights = state.nudge.weights
        weights = canonical_weights(raw_weights)
        parts: dict[str, dict[str, float]] = {}
        degraded: list[str] = []
        for signal in signals:
            weight = float(weights.get(signal.name, 0))
            try:
                raw = float(signal.compute(window))
            except Exception as exc:
                degraded.append(signal.name)
                if signal.name not in state.degraded_logged:
                    state.degraded_logged.add(signal.name)
                    log_event(
                        "WARNING",
                        "signal.failed",
                        channel=view.channel_key,
                        signal=signal.name,
                        error=str(exc),
                    )
                continue
            state.degraded_logged.discard(signal.name)
            baseline = state.baselines.setdefault(
                signal.name,
                RollingBaseline(
                    horizon=self.config.baseline_minutes * 60,
                    min_samples=self.min_baseline_samples,
                ),
            )
            score = baseline.add(now, raw)
            parts[signal.name] = {"raw": raw, "z": round(score, 3), "weight": weight}
        tick = combine(parts, degraded)
        threshold = state.nudge.threshold if state.nudge else channel_threshold(view.thresholds)
        release = threshold * self.config.hysteresis
        phase = state.tracker.push(
            now,
            tick.score,
            threshold=threshold,
            release=release,
            max_len=self.config.clip_max_seconds,
        )
        log_event(
            "DEBUG",
            "signal.tick",
            channel=view.channel_key,
            score=round(tick.score, 3),
            threshold=threshold,
            degraded=degraded,
        )
        if phase == "rising" and (state.peak_tick is None or tick.score >= state.peak_tick.score):
            state.peak_tick = tick
        if phase == "fire":
            samples = list(state.tracker.samples)
            peak = state.peak_tick or tick
            state.peak_tick = None
            state.tracker.reset()
            handled = False
            if self.editorial is not None:
                try:
                    handled = await self.editorial.on_fire(state, samples, peak, now)
                except Exception as exc:
                    log_event("ERROR", "editorial.fire_failed", channel=view.channel_key, error=str(exc))
                    handled = False
            if not handled:
                state.cooldown_until = now + channel_cooldown(view.thresholds)
                await self._cut(state, samples, peak)
            elif state.cooldown_until < now + 1:
                state.cooldown_until = now + float(self.config.editorial_after_seconds) + 2
        return tick

    async def learn_from_decisions(self) -> None:
        rows = await self.repo.list_decisions()
        by_channel = await self._channel_of_clips()
        for state in self._states.values():
            if state.nudge is None:
                continue
            relevant = [row for row in rows if by_channel.get(row["clip_id"]) == state.view.channel_key]
            if len(relevant) <= state.seen_decisions:
                continue
            fresh = relevant[state.seen_decisions :]
            state.seen_decisions = len(relevant)
            changed = False
            for row in fresh:
                if row.get("note") == "hotkey":
                    continue
                changed = state.nudge.observe(str(row.get("action") or "")) or changed
            if changed:
                save_nudge(calibration_path(self.config.resolved_database_path()), state.view.channel_key, state.nudge)

    async def _poll_live(self, state: _ChannelRuntime, now: float) -> None:
        if not state.view.video_id:
            return
        if now - state.last_chat_poll >= 4:
            state.last_chat_poll = now
            try:
                from cliphunter.signals.youtube_chat import YouTubeChatClient

                if state.chat_client is None:
                    state.chat_client = YouTubeChatClient(state.view.video_id)
                messages = await asyncio.wait_for(state.chat_client.poll(now), timeout=3)  # type: ignore[attr-defined]
                self.note_chat(state.view.channel_key, messages)
            except Exception as exc:
                log_event("WARNING", "signal.chat_failed", channel=state.view.channel_key, error=str(exc))
        if now - state.last_viewer_poll >= 60:
            state.last_viewer_poll = now
            try:
                count = await asyncio.wait_for(_fetch_viewers(state.view.video_id), timeout=8)
                if count is not None:
                    self.note_viewers(state.view.channel_key, now, count)
            except Exception as exc:
                log_event("WARNING", "signal.viewers_failed", channel=state.view.channel_key, error=str(exc))

    async def _window(self, state: _ChannelRuntime, now: float) -> SignalWindow:
        start = now - self.config.signal_window_seconds
        messages = [item for item in state.messages if start <= item.t <= now]
        audio, scene = await self._media(state)
        delta = None
        if len(state.viewers) >= 2:
            delta = float(state.viewers[-1][1] - state.viewers[-2][1])
        return SignalWindow(
            channel_key=state.view.channel_key,
            start=start,
            end=now,
            messages=messages,
            audio_rms=audio,
            scene_score=scene,
            viewer_delta=delta,
        )

    async def _media(self, state: _ChannelRuntime) -> tuple[float | None, float | None]:
        path = state.view.index_path
        if not path.exists():
            return None, None
        try:
            index = SegmentIndex.load(path)
        except (OSError, ValueError):
            return None, None
        if not index.segments:
            return None, None
        latest = index.segments[-1]
        media = path.parent / latest.file
        if not media.exists():
            return None, None
        key = str(media)
        if key not in state.audio_cache:
            try:
                state.audio_cache[key] = await audio_rms(media)
            except Exception as exc:
                log_event("WARNING", "signal.audio_failed", channel=state.view.channel_key, error=str(exc))
                state.audio_cache[key] = None  # type: ignore[assignment]
        if key not in state.scene_cache:
            try:
                state.scene_cache[key] = await scene_score(media)
            except Exception as exc:
                log_event("WARNING", "signal.scene_failed", channel=state.view.channel_key, error=str(exc))
                state.scene_cache[key] = None  # type: ignore[assignment]
        # Keep caches small.
        if len(state.audio_cache) > 40:
            for old in list(state.audio_cache)[:-20]:
                state.audio_cache.pop(old, None)
                state.scene_cache.pop(old, None)
        return state.audio_cache.get(key), state.scene_cache.get(key)

    def _signals(self, view: LiveView) -> list[Signal]:
        return [
            ChatVelocity(),
            EmoteBurst(view.emotes),
            AudioEnergy(),
            SceneChange(),
            ViewerDelta(),
        ]

    async def _cut(self, state: _ChannelRuntime, samples: list[tuple[float, float]], tick: ScoreTick) -> None:
        if not state.view.index_path.exists():
            return
        index = SegmentIndex.load(state.view.index_path)
        start, end, peak = choose_bounds(
            samples,
            min_seconds=self.config.clip_min_seconds,
            max_seconds=self.config.clip_max_seconds,
            target_seconds=self.config.clip_target_seconds,
            pre_roll=self.config.clip_pre_roll_seconds,
        )
        selected = segments_for_window(
            index,
            start,
            end,
            peak,
            min_seconds=self.config.clip_min_seconds,
            max_seconds=self.config.clip_max_seconds,
        )
        if not selected:
            log_event("WARNING", "auto.no_segments", channel=state.view.channel_key)
            return
        breakdown = {
            "score": round(tick.score, 3),
            "signals": tick.parts,
            "degraded": tick.degraded,
            "peak": peak,
        }
        mode = getattr(state, "editorial_mode", None)
        if mode:
            breakdown["mode"] = mode
        output = self.config.resolved_clips_dir() / f"{state.view.channel_key}_auto_{int(peak)}.mp4"
        try:
            result = await create_clip(
                index_path=state.view.index_path,
                seconds=max(self.config.clip_min_seconds, min(self.config.clip_max_seconds, end - start)),
                output=output,
                repo=self.repo,
                channel_id=state.view.channel_db_id,
                authorization=state.view.authorization,
                credit=state.view.credit,
                status="pending",
                breakdown=breakdown,
                kind="auto",
                signal_score=tick.score,
                selected=selected,
            )
        except Exception as exc:
            log_event("ERROR", "auto.cut_failed", channel=state.view.channel_key, error=str(exc))
            return
        payload = {
            "channel_key": state.view.channel_key,
            "clip_id": result.clip_id,
            "start": selected[0].wall_start,
            "end": selected[-1].wall_end,
            "peak_score": round(tick.score, 3),
            "breakdown": breakdown,
            "transcript": None,
            "path": str(result.path),
        }
        await self.bus.publish(Event(MOMENT_CANDIDATE, payload))
        if self.on_moment is not None:
            try:
                self.on_moment()
            except Exception as exc:
                log_event("WARNING", "review.wake_failed", error=str(exc))
        log_event("INFO", "moment.candidate", channel=state.view.channel_key, clip_id=result.clip_id, score=round(tick.score, 3))
        if self.config.transcript_on_trigger and result.clip_id is not None:
            asyncio.create_task(
                self._attach_transcript(result.clip_id, result.path),
                name=f"transcript-{result.clip_id}",
            )

    async def _attach_transcript(self, clip_id: int, media: Path) -> None:
        from cliphunter.signals.transcript import transcribe_clip

        text = await transcribe_clip(media=media, model_name=self.config.whisper_model)
        if not text:
            return
        row = await self.repo.get_clip(clip_id)
        if row is None:
            return
        attribution = json.loads(row["attribution_json"] or "{}")
        attribution["transcript"] = text
        await self.repo.update_clip(clip_id, attribution=attribution)
        await self.bus.publish(
            Event(MOMENT_CANDIDATE, {"clip_id": clip_id, "transcript": text, "update": True})
        )

    async def _channel_of_clips(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for clip in await self.repo.list_clips():
            attribution = json.loads(clip.get("attribution_json") or "{}")
            key = attribution.get("channel_key")
            if key:
                mapping[int(clip["id"])] = str(key)
        return mapping


async def _fetch_viewers(video_id: str) -> int | None:
    import re

    import httpx

    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        response = await client.get(
            "https://www.youtube.com/watch",
            params={"v": video_id},
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "es-ES,en;q=0.8"},
        )
    match = re.search(r'"concurrentViewers"\s*:\s*"(\d+)"', response.text)
    if not match:
        return None
    return int(match.group(1))


def _key(channel: ChannelConfig) -> str:
    from cliphunter.config import channel_key

    return channel_key(channel)
