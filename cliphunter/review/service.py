"""Review queue. Telegram and the local page share one decision path.

A failure here is logged and swallowed. Recording does not depend on it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cliphunter.clip import plan_from_segments, mux_plan
from cliphunter.config import AppConfig
from cliphunter.env import EnvSettings
from cliphunter.index import SegmentIndex
from cliphunter.jsonlog import log_event
from cliphunter.review.adjust import shift_window
from cliphunter.review.localapp import LocalNotifier
from cliphunter.review.messages import ADJUST_OPS, ready_sidecar_text
from cliphunter.review.state import load_state, state_path
from cliphunter.review.telegram import TelegramNotifier
from cliphunter.storage.repo import Repo

Recut = Callable[..., Awaitable[dict[str, Any]]]


def _breakdown(row: dict[str, Any], attribution: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _loads(row.get("breakdown_json"))
    if parsed:
        return parsed
    value = attribution.get("breakdown")
    return value if isinstance(value, dict) and value else None


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def clip_row_to_item(row: dict[str, Any]) -> dict[str, Any]:
    attribution = _loads(row.get("attribution_json"))
    breakdown = _loads(row.get("breakdown_json")) or attribution.get("breakdown")
    if breakdown == {}:
        breakdown = attribution.get("breakdown")
    score = row.get("signal_score")
    return {
        "clip_id": int(row["id"]),
        "path": row.get("file_path") or "",
        "channel": attribution.get("streamer") or attribution.get("platform") or "",
        "clip_start": attribution.get("clip_start"),
        "clip_end": attribution.get("clip_end"),
        "signal_score": score,
        "viral_score": attribution.get("viral_score"),
        "tier": attribution.get("tier"),
        "title": row.get("title") or attribution.get("title"),
        "hook": row.get("hook") or attribution.get("hook"),
        "category": attribution.get("category"),
        "participants": attribution.get("participants") or [],
        "why": attribution.get("why"),
        "editorial_mode": attribution.get("editorial_mode"),
        "breakdown": breakdown if isinstance(breakdown, dict) and breakdown else None,
        "credit": attribution.get("credit"),
        "attribution": attribution,
    }


async def default_recut(
    index_path: Path,
    files: list[str],
    edge: str,
    delta: float,
    output: Path,
    previous: dict[str, Any],
) -> dict[str, Any]:
    index = SegmentIndex.load(index_path)
    selected = shift_window(index, files, edge=edge, delta_seconds=delta)
    plan = plan_from_segments(index, selected, requested_seconds=sum(segment.duration for segment in selected))
    for key in ("authorization", "credit", "breakdown", "calibration_weight", "channel_context"):
        if previous.get(key) not in (None, "") and key not in plan.attribution:
            plan.attribution[key] = previous[key]
    plan.attribution["buffer_index"] = str(index_path)
    duration = await mux_plan(plan, output)
    plan.attribution["file_path"] = str(output.resolve())
    output.with_suffix(".json").write_text(
        json.dumps(plan.attribution, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"attribution": plan.attribution, "duration": duration, "path": output}


class ReviewRuntime:
    def __init__(
        self,
        *,
        repo: Repo,
        config: AppConfig,
        env: EnvSettings | None = None,
        transport: Callable[..., Awaitable[dict]] | None = None,
        recut: Recut | None = None,
        enable_local: bool = True,
    ) -> None:
        self.repo = repo
        self.config = config
        self.ready_dir = config.resolved_ready_dir()
        self._recut = recut or default_recut
        self._wake = asyncio.Event()
        token = env.telegram_bot_token if env is not None else None
        path = state_path(config)
        self.telegram = None
        if token and load_state(path).chat_id is not None:
            self.telegram = TelegramNotifier(token, path, self.apply_code, transport=transport)
        self.local = LocalNotifier(self, config.review_port) if enable_local else None

    def wake(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [
            asyncio.create_task(self._isolate(self._scan_loop(stop)), name="review-scan"),
        ]
        if self.telegram is not None:
            tasks.append(asyncio.create_task(self._isolate(self.telegram.run(stop)), name="review-telegram"))
        if self.local is not None:
            tasks.append(asyncio.create_task(self._isolate(self.local.serve(stop)), name="review-local"))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.telegram is not None:
                await self.telegram.aclose()

    async def _isolate(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("ERROR", "review.isolated", error=str(exc))

    async def _scan_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:
                log_event("WARNING", "review.scan_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2)
            except TimeoutError:
                pass
            self._wake.clear()

    async def scan_once(self) -> None:
        rows = await self.repo.list_clips()
        for row in rows:
            if row.get("status") != "pending" or row.get("review_claimed"):
                continue
            item = clip_row_to_item(row)
            if self.local is not None:
                self.local.announce(item)
            if self.telegram is None:
                continue
            if not await self.repo.claim_review(int(row["id"])):
                continue
            try:
                await self.telegram.send(item)
            except Exception as exc:
                log_event("WARNING", "review.send_failed", clip_id=row["id"], error=str(exc))
                await self.repo.release_review(int(row["id"]))

    async def pending_items(self) -> list[dict[str, Any]]:
        rows = await self.repo.list_clips()
        return [clip_row_to_item(row) for row in rows if row.get("status") == "pending"]

    async def media_path(self, clip_id: int) -> Path | None:
        row = await self.repo.get_clip(clip_id)
        if row is None or not row.get("file_path"):
            return None
        return Path(row["file_path"])

    async def apply_code(self, clip_id: int, code: str) -> None:
        if code == "a":
            await self.approve(clip_id)
            return
        if code == "ask":
            return
        if code == "r":
            await self.reject(clip_id)
            return
        if code.startswith("why:"):
            reason = code.split(":", 1)[1]
            await self.reject(clip_id, reason=reason)
            return
        if code not in ADJUST_OPS:
            raise ValueError(f"acción desconocida: {code}")
        edge, delta = ADJUST_OPS[code]
        await self.adjust(clip_id, edge, delta)

    async def approve(self, clip_id: int) -> Path:
        row = await self._require(clip_id)
        attribution = _loads(row.get("attribution_json"))
        if not row.get("file_path"):
            raise FileNotFoundError(f"el clip {clip_id} no tiene archivo")
        source = Path(row["file_path"])
        if not source.exists():
            raise FileNotFoundError(f"no está el clip {source}")
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        dest = self.ready_dir / source.name
        rendered = False
        if attribution.get("editorial_mode") == "editorial":
            rendered = await self._render_vertical(source, dest, attribution, row)
        if not rendered:
            if dest.exists():
                dest.unlink()
            shutil.copy2(source, dest)
        dest.with_suffix(".txt").write_text(ready_sidecar_text(attribution), encoding="utf-8")
        breakdown = _breakdown(row, attribution)
        participants = attribution.get("participants") if isinstance(attribution.get("participants"), list) else None
        await self.repo.insert_decision(
            clip_id=clip_id,
            action="approve",
            adjust_seconds=0,
            note="review",
            weight=row.get("weight"),
            breakdown=breakdown,
            category=attribution.get("category"),
            participants=participants,
        )
        attribution["ready_path"] = str(dest)
        await self.repo.update_clip(clip_id, status="approved", attribution=attribution)
        log_event("INFO", "review.approved", clip_id=clip_id, ready=str(dest))
        return dest

    async def reject(self, clip_id: int, reason: str | None = None) -> None:
        row = await self._require(clip_id)
        attribution = _loads(row.get("attribution_json"))
        participants = attribution.get("participants") if isinstance(attribution.get("participants"), list) else None
        await self.repo.insert_decision(
            clip_id=clip_id,
            action="reject",
            adjust_seconds=0,
            note="review",
            weight=row.get("weight"),
            breakdown=_breakdown(row, attribution),
            reason=reason,
            category=attribution.get("category"),
            participants=participants,
        )
        await self.repo.update_clip(clip_id, status="rejected")
        log_event("INFO", "review.rejected", clip_id=clip_id, reason=reason)

    async def _render_vertical(self, source: Path, dest: Path, attribution: dict[str, Any], row: dict[str, Any]) -> bool:
        from cliphunter.editorial.bounds import Line
        from cliphunter.editorial.vertical import render_vertical
        from cliphunter.timeutil import parse_iso

        try:
            lines = [
                Line(float(item["start"]), float(item["end"]), str(item.get("text") or ""))
                for item in (attribution.get("transcript_lines") or [])
                if isinstance(item, dict)
            ]
            clip_start = 0.0
            if attribution.get("clip_start"):
                clip_start = parse_iso(str(attribution["clip_start"])).timestamp()
            await render_vertical(
                source,
                dest,
                title=str(attribution.get("title") or ""),
                sujeto=attribution.get("sujeto"),
                lines=lines,
                clip_start=clip_start,
                duration=float(row.get("duration_seconds") or attribution.get("actual_seconds") or 30),
                style=self.config.title_style,
            )
            return True
        except Exception as exc:
            log_event("WARNING", "editorial.vertical_failed", error=str(exc))
            if dest.exists():
                dest.unlink()
            return False

    async def adjust(self, clip_id: int, edge: str, delta: float) -> None:
        row = await self._require(clip_id)
        attribution = _loads(row.get("attribution_json"))
        index_value = attribution.get("buffer_index")
        files = list(attribution.get("source_segments") or [])
        if not index_value or not files:
            raise ValueError("el clip no guarda el índice del buffer para reajustar")
        source = Path(row["file_path"])
        output = source.with_name(f"{source.stem}_{edge}_{int(delta):+d}.mp4")
        recut = await self._recut(Path(index_value), files, edge, delta, output, attribution)
        new_attribution = dict(recut["attribution"])
        new_attribution["credit"] = attribution.get("credit")
        new_attribution["breakdown"] = attribution.get("breakdown")
        await self.repo.insert_decision(
            clip_id=clip_id,
            action="adjust",
            adjust_seconds=delta,
            note=edge,
            weight=row.get("weight"),
            breakdown=_breakdown(row, attribution),
        )
        await self.repo.update_clip(
            clip_id,
            status="pending",
            attribution=new_attribution,
            file_path=str(Path(recut["path"]).resolve()),
            duration_seconds=float(recut["duration"]),
        )
        log_event("INFO", "review.adjusted", clip_id=clip_id, edge=edge, delta=delta)
        await self._resend(clip_id)

    async def _resend(self, clip_id: int) -> None:
        if self.telegram is None:
            return
        await self.repo.release_review(clip_id)
        if not await self.repo.claim_review(clip_id):
            return
        row = await self.repo.get_clip(clip_id)
        if row is None:
            return
        try:
            await self.telegram.send(clip_row_to_item(row))
        except Exception as exc:
            log_event("WARNING", "review.resend_failed", clip_id=clip_id, error=str(exc))
            await self.repo.release_review(clip_id)

    async def _require(self, clip_id: int) -> dict[str, Any]:
        row = await self.repo.get_clip(clip_id)
        if row is None:
            raise ValueError(f"no existe el clip {clip_id}")
        if row.get("status") not in {"pending", "ready"}:
            raise ValueError(f"el clip {clip_id} ya está {row.get('status')}")
        return row


def build_review_runtime(
    *,
    repo: Repo,
    config: AppConfig,
    env: EnvSettings,
    transport: Callable[..., Awaitable[dict]] | None = None,
    enable_local: bool = True,
) -> ReviewRuntime:
    return ReviewRuntime(repo=repo, config=config, env=env, transport=transport, enable_local=enable_local)
