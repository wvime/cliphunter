"""Small automatic threshold moves from consecutive review decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from cliphunter.atomic import atomic_write_json
from cliphunter.jsonlog import log_event


@dataclass
class ChannelNudge:
    threshold: float
    floor: float
    ceiling: float
    consecutive_rejections: int = 0
    consecutive_approvals: int = 0
    weights: dict[str, float] = field(default_factory=dict)

    def observe(self, action: str) -> bool:
        changed = False
        if action == "reject":
            self.consecutive_rejections += 1
            self.consecutive_approvals = 0
            if self.consecutive_rejections >= 3:
                updated = min(self.ceiling, round(self.threshold * 1.08, 3))
                self.consecutive_rejections = 0
                changed = updated != self.threshold
                self.threshold = updated
        elif action == "approve":
            self.consecutive_approvals += 1
            self.consecutive_rejections = 0
            if self.consecutive_approvals >= 2:
                updated = max(self.floor, round(self.threshold * 0.95, 3))
                self.consecutive_approvals = 0
                changed = updated != self.threshold
                self.threshold = updated
        return changed


def calibration_path(database: Path) -> Path:
    return database.parent / "calibration.json"


def load_nudges(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    channels = payload.get("channels") if isinstance(payload, dict) else None
    return channels if isinstance(channels, dict) else {}


def save_nudge(path: Path, channel_key: str, nudge: ChannelNudge) -> None:
    channels = load_nudges(path)
    channels[channel_key] = {
        "threshold": nudge.threshold,
        "floor": nudge.floor,
        "ceiling": nudge.ceiling,
        "consecutive_rejections": nudge.consecutive_rejections,
        "consecutive_approvals": nudge.consecutive_approvals,
        "weights": nudge.weights,
    }
    atomic_write_json(path, {"channels": channels})
    log_event(
        "INFO",
        "calibrate.nudge",
        channel=channel_key,
        threshold=nudge.threshold,
        rejections=nudge.consecutive_rejections,
        approvals=nudge.consecutive_approvals,
    )
