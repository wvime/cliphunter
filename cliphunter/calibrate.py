"""Suggest thresholds and weights from approvals and rejections.

Hotkey decisions already carry a higher ``weight`` (3.0) and therefore pull
the averages more than a normal review.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cliphunter.config import AppConfig, channel_key, load_config
from cliphunter.signals.aggregate import DEFAULT_WEIGHTS, canonical_weights, channel_threshold
from cliphunter.signals.nudge import calibration_path, load_nudges
from cliphunter.storage.repo import SqliteRepo


def suggest(records: list[dict], *, threshold: float, weights: dict[str, float]) -> dict:
    approved = [row for row in records if row["action"] == "approve"]
    rejected = [row for row in records if row["action"] == "reject"]
    suggested_threshold = threshold
    if approved and rejected:
        low = _wmean([(row["score"], row["weight"]) for row in rejected])
        high = _wmean([(row["score"], row["weight"]) for row in approved])
        if low is not None and high is not None and high > low:
            suggested_threshold = round((low + high) / 2, 3)
    elif approved and not rejected:
        high = _wmean([(row["score"], row["weight"]) for row in approved])
        if high is not None:
            suggested_threshold = round(min(threshold, high), 3)
    new_weights = dict(weights)
    for name in list(weights):
        delta = _signal_gap(approved, rejected, name)
        if delta is None:
            continue
        # A signal that is hotter on approvals than rejections earns weight.
        scale = 1 + 0.15 * max(-1.0, min(1.0, delta / 2))
        new_weights[name] = max(0.02, weights[name] * scale)
    total = sum(new_weights.values()) or 1.0
    new_weights = {name: round(value / total, 3) for name, value in new_weights.items()}
    return {"threshold": suggested_threshold, "weights": new_weights, "approvals": len(approved), "rejections": len(rejected)}


def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    weight = sum(item[1] for item in pairs)
    if weight <= 0:
        return None
    return sum(value * item_weight for value, item_weight in pairs) / weight


def _signal_gap(approved: list[dict], rejected: list[dict], name: str) -> float | None:
    left = _wmean([(row["signals"].get(name, 0.0), row["weight"]) for row in approved if name in row["signals"]])
    right = _wmean([(row["signals"].get(name, 0.0), row["weight"]) for row in rejected if name in row["signals"]])
    if left is None or right is None:
        return None
    return left - right


def records_from_rows(clips: list[dict], decisions: list[dict]) -> dict[str, list[dict]]:
    by_id = {int(clip["id"]): clip for clip in clips}
    grouped: dict[str, list[dict]] = {}
    for decision in decisions:
        if decision.get("note") == "adjust":
            continue
        clip = by_id.get(int(decision["clip_id"]))
        if clip is None:
            continue
        attribution = json.loads(clip.get("attribution_json") or "{}")
        breakdown = json.loads(clip.get("breakdown_json") or "{}") if clip.get("breakdown_json") else {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        signals = {}
        for name, item in (breakdown.get("signals") or {}).items():
            if isinstance(item, dict) and "z" in item:
                signals[name] = float(item["z"])
        key = str(attribution.get("channel_key") or "")
        if not key:
            continue
        grouped.setdefault(key, []).append(
            {
                "action": decision.get("action"),
                "weight": float(decision.get("weight") or 1),
                "score": float(breakdown.get("score") or clip.get("signal_score") or 0),
                "signals": signals,
            }
        )
    return grouped


def calibrate_command(config_path: Path, *, apply: bool = False, canal: str | None = None) -> int:
    config = load_config(config_path)
    repo = SqliteRepo(config.resolved_database_path())

    async def _load() -> tuple[list[dict], list[dict]]:
        await repo.init()
        try:
            return await repo.list_clips(), await repo.list_decisions()
        finally:
            await repo.close()

    clips, decisions = asyncio.run(_load())
    grouped = records_from_rows(clips, decisions)
    if canal:
        wanted = {canal, canal.lstrip("@")}
        grouped = {key: rows for key, rows in grouped.items() if key in wanted or key.endswith("_" + canal)}
    if not grouped:
        print("calibrate: no hay decisiones con breakdown todavía.")
        return 0
    overrides = load_nudges(calibration_path(config.resolved_database_path()))
    applied: dict[str, dict] = {}
    for key, rows in grouped.items():
        channel = _match_channel(config, key)
        base_weights = canonical_weights(channel.weights if channel else None)
        current_threshold = channel_threshold(channel.thresholds if channel else None)
        if key in overrides and "threshold" in overrides[key]:
            current_threshold = float(overrides[key]["threshold"])
        if key in overrides and overrides[key].get("weights"):
            base_weights = canonical_weights(overrides[key]["weights"])
        suggestion = suggest(rows, threshold=current_threshold, weights=base_weights)
        print(f"canal {key}: umbral {current_threshold} -> {suggestion['threshold']}")
        print(f"  pesos {suggestion['weights']} (aprobados {suggestion['approvals']}, rechazados {suggestion['rejections']})")
        applied[key] = {
            "threshold": suggestion["threshold"],
            "weights": suggestion["weights"],
            "floor": round(current_threshold * 0.5, 3),
            "ceiling": round(max(current_threshold, suggestion["threshold"]) * 2, 3),
            "consecutive_rejections": int(overrides.get(key, {}).get("consecutive_rejections", 0)),
            "consecutive_approvals": int(overrides.get(key, {}).get("consecutive_approvals", 0)),
        }
    if apply:
        path = calibration_path(config.resolved_database_path())
        from cliphunter.atomic import atomic_write_json

        merged = load_nudges(path)
        merged.update(applied)
        atomic_write_json(path, {"channels": merged})
        print(f"calibrate: aplicado en {path}")
    return 0


def _match_channel(config: AppConfig, key: str):
    for channel in config.channels:
        if channel_key(channel) == key:
            return channel
    return None
