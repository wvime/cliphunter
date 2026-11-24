"""Live editorial pass. A failure here falls back to the signal cutter."""

from __future__ import annotations

import asyncio

from cliphunter.atomic import atomic_write_json
from cliphunter.bus import MOMENT_CANDIDATE, Event
from cliphunter.config import AppConfig
from cliphunter.editorial.audio import read_pcm, s16le_to_floats, transcribe_samples, whisper_available
from cliphunter.editorial.bounds import Line, clip_bounds
from cliphunter.editorial.cues import find_cues
from cliphunter.editorial.cut import cut_precise
from cliphunter.editorial.feedback import feedback_block, summarize_feedback
from cliphunter.editorial.judge import ClaudeJudge
from cliphunter.env import EnvSettings
from cliphunter.index import SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.signals.aggregate import channel_cooldown
from cliphunter.timeutil import parse_iso


def editorial_active(config: AppConfig, env: EnvSettings) -> bool:
    mode = getattr(config, "editorial", "auto")
    if mode == "off":
        return False
    if mode == "on":
        return True
    return bool(env.anthropic_api_key)


class EditorialDirector:
    def __init__(self, config: AppConfig, env: EnvSettings, repo, engine) -> None:
        self.config = config
        self.repo = repo
        self.engine = engine
        self.judge = ClaudeJudge(
            env.anthropic_api_key,
            model=config.editorial_model,
            minimum=config.editorial_min_score,
        )
        self.lines: dict[str, list[Line]] = {}
        self.seen_media: dict[str, set[str]] = {}
        self.pending: dict[str, dict] = {}
        self.last_sweep: dict[str, float] = {}
        self._names: list[str] = []
        self._context = ""
        self._feedback = ""
        self._context_at = 0.0

    async def poll(self, state, now: float) -> None:
        await self._transcribe_new(state)
        await self._finish_pending(state, now)
        key = state.view.channel_key
        if key in self.pending or now < state.cooldown_until:
            return
        if now - self.last_sweep.get(key, 0.0) < self.config.editorial_sweep_seconds:
            return
        self.last_sweep[key] = now
        cues = find_cues(self._cue_lines(state, now), await self._known_names(now))
        if not cues:
            return
        if not self.judge.api_key:
            return
        self.pending[key] = {"ready_at": now + self.config.editorial_after_seconds, "tick": None, "trigger": ",".join(cues), "at": now}

    async def on_fire(self, state, samples, peak_tick, now: float) -> bool:
        if not self.judge.api_key:
            state.editorial_mode = "signal-only"
            return False
        self.pending[state.view.channel_key] = {
            "ready_at": now + self.config.editorial_after_seconds,
            "tick": peak_tick,
            "trigger": "señal",
            "at": now,
            "samples": samples,
        }
        await self._finish_pending(state, now)
        return True

    async def _finish_pending(self, state, now: float) -> None:
        key = state.view.channel_key
        pending = self.pending.get(key)
        if pending is None or now + 1e-6 < pending["ready_at"]:
            return
        self.pending.pop(key, None)
        lines = self._window_lines(state, now)
        t0 = lines[0].start if lines else now - self.config.editorial_before_seconds
        packet = {
            "t0": t0,
            "transcript": [
                {"start": round(line.start - t0, 2), "end": round(line.end - t0, 2), "text": line.text}
                for line in lines
            ],
            "chat": [message.text for message in state.messages if now - self.config.editorial_before_seconds <= message.t <= now],
            "signals": (pending.get("tick").parts if pending.get("tick") is not None else {}),
            "signal_score": (pending.get("tick").score if pending.get("tick") is not None else None),
            "cues": pending.get("trigger"),
            "context": await self._context_text(now),
            "feedback": self._feedback,
        }
        verdict = await self.judge.judge(packet)
        if verdict.mode != "editorial":
            state.editorial_mode = "signal-only"
            if pending.get("tick") is not None:
                await self.engine._cut(state, pending.get("samples") or [], pending["tick"])
            return
        if not verdict.keep:
            state.cooldown_until = now + channel_cooldown(state.view.thresholds)
            log_event(
                "INFO",
                "editorial.rejected",
                channel=key,
                score=verdict.total,
                tier=verdict.tier,
                why=verdict.why,
                storyline=verdict.storyline_note,
            )
            return
        if verdict.setup_start is None or verdict.end is None:
            state.editorial_mode = "signal-only"
            if pending.get("tick") is not None:
                await self.engine._cut(state, pending.get("samples") or [], pending["tick"])
            return
        start, end, payoff = clip_bounds(
            lines,
            t0 + verdict.setup_start,
            t0 + (verdict.payoff if verdict.payoff is not None else verdict.setup_start),
            t0 + verdict.end,
            min_seconds=self.config.clip_min_seconds,
            max_seconds=self.config.editorial_max_seconds,
            story_max_seconds=self.config.editorial_story_max_seconds,
            storyline=verdict.storyline,
        )
        await self._save(state, start, end, payoff, verdict, pending.get("trigger") or "")
        state.cooldown_until = now + channel_cooldown(state.view.thresholds)

    async def _save(self, state, start: float, end: float, payoff: float, verdict, trigger: str) -> None:
        output = self.config.resolved_clips_dir() / f"{state.view.channel_key}_edit_{int(start)}.mp4"
        try:
            _selected, attribution, duration = await cut_precise(
                index_path=state.view.index_path,
                start=start,
                end=end,
                output=output,
            )
        except Exception as exc:
            log_event("ERROR", "editorial.cut_failed", channel=state.view.channel_key, error=str(exc))
            return
        attribution.update(
            {
                "channel_key": state.view.channel_key,
                "editorial_mode": "editorial",
                "title": verdict.title,
                "hook": verdict.hook,
                "why": verdict.why,
                "category": verdict.category,
                "participants": verdict.participants,
                "sujeto": verdict.sujeto,
                "tier": verdict.tier,
                "viral_score": verdict.total,
                "storyline": verdict.storyline_note if verdict.storyline else None,
                "payoff": payoff,
                "trigger": trigger,
                "prompt_version": verdict.prompt_version,
                "transcript_lines": [
                    {"start": line.start, "end": line.end, "text": line.text}
                    for line in self.lines.get(state.view.channel_key, [])
                    if line.end >= start and line.start <= end
                ],
            }
        )
        if state.view.credit:
            attribution["credit"] = state.view.credit
        if state.view.authorization:
            attribution["authorization"] = state.view.authorization
        breakdown = verdict.public_dict()
        breakdown["trigger"] = trigger
        sidecar = output.with_suffix(".json")
        atomic_write_json(sidecar, attribution)
        session_id = attribution.get("session_id")
        if not isinstance(session_id, int):
            session_id = None
        elif not await self.repo.session_exists(session_id):
            session_id = None
        clip_id = await self.repo.insert_clip(
            channel_id=state.view.channel_db_id,
            session_id=session_id,
            kind="auto",
            status="pending",
            attribution=attribution,
            file_path=str(output.resolve()),
            duration_seconds=duration,
            signal_score=verdict.total,
            breakdown=breakdown,
            title=verdict.title,
            hook=verdict.hook,
        )
        attribution["clip_id"] = clip_id
        atomic_write_json(sidecar, attribution)
        await self.engine.bus.publish(
            Event(
                MOMENT_CANDIDATE,
                {
                    "channel_key": state.view.channel_key,
                    "clip_id": clip_id,
                    "path": str(output),
                    "breakdown": breakdown,
                    "editorial": True,
                },
            )
        )
        if self.engine.on_moment is not None:
            self.engine.on_moment()
        log_event("INFO", "editorial.candidate", channel=state.view.channel_key, clip_id=clip_id, score=verdict.total)

    async def _transcribe_new(self, state) -> None:
        if not whisper_available() or not state.view.index_path.exists():
            return
        try:
            index = SegmentIndex.load(state.view.index_path)
        except (OSError, ValueError):
            return
        if not index.segments:
            return
        seen = self.seen_media.setdefault(state.view.channel_key, set())
        latest = index.segments[-1]
        media = state.view.index_path.parent / latest.file
        key = str(media)
        if key in seen or not media.exists():
            return
        seen.add(key)
        try:
            pcm = await read_pcm(media)
            samples = s16le_to_floats(pcm)
            pieces = await asyncio.to_thread(
                transcribe_samples,
                samples,
                model_name=self.config.whisper_model,
                language="es",
            )
        except Exception as exc:
            log_event("WARNING", "editorial.transcript_failed", channel=state.view.channel_key, error=str(exc))
            return
        wall = parse_iso(latest.wall_start).timestamp()
        bucket = self.lines.setdefault(state.view.channel_key, [])
        for start, end, text in pieces:
            bucket.append(Line(wall + start, wall + end, text))
        cutoff = wall - self.config.baseline_minutes * 60
        self.lines[state.view.channel_key] = [line for line in bucket if line.end >= cutoff]

    def _window_lines(self, state, now: float) -> list[Line]:
        start = now - self.config.editorial_before_seconds
        end = now + 0.5
        return [line for line in self.lines.get(state.view.channel_key, []) if line.end >= start and line.start <= end]

    def _cue_lines(self, state, now: float) -> list[Line]:
        lines = list(self._window_lines(state, now))
        start = now - self.config.editorial_before_seconds
        for message in state.messages:
            if start <= message.t <= now and message.text.strip():
                lines.append(Line(message.t, message.t + 0.4, message.text))
        return lines

    async def _known_names(self, now: float) -> list[str]:
        await self._context_text(now)
        return list(self._names)

    async def _context_text(self, now: float) -> str:
        if now - self._context_at < 60 and self._context_at:
            return self._context
        people = await self.repo.list_participants()
        links = await self.repo.list_relationships()
        stories = await self.repo.list_storylines()
        decisions = await self.repo.list_decisions()
        publish = await self.repo.list_publish_results()
        names: list[str] = []
        rows = []
        for person in people:
            flag = "verificado" if person.get("verified") else "rumor"
            nick = ", ".join(person.get("nicknames") or [])
            names.append(str(person["name"]))
            names.extend(person.get("nicknames") or [])
            rows.append(
                f"- {person['name']} ({flag}, fuente: {person.get('source') or 'sin fuente'})"
                + (f" apodos: {nick}" if nick else "")
                + (f". {person.get('description')}" if person.get("description") else "")
            )
        for link in links:
            flag = "verificado" if link.get("verified") else "rumor"
            rows.append(
                f"- relación {flag}: {link['left_name']} / {link['right_name']} ({link['kind']})"
                + (f" fuente: {link.get('source')}" if link.get("source") else "")
            )
        for story in stories:
            flag = "verificado" if story.get("verified") else "rumor"
            rows.append(f"- storyline {flag}: {story['title']}. {story.get('detail') or ''}")
        summary = summarize_feedback(decisions, publish, minimum=self.config.editorial_min_score)
        self._feedback = feedback_block(summary)
        self._names = names
        self._context = "\n".join(rows) or "Sin ficha de participantes. No inventes relaciones."
        self._context_at = now
        return self._context
