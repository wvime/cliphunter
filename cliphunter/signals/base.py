"""Real-time signal interface. Detectors arrive in a later phase.

They subscribe to ``segment.ready`` and return hits the scorer can read.
Phase 1 publishes the event and does not run detectors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class SignalHit(BaseModel):
    name: str
    score: float
    start_offset: float = 0
    end_offset: float = 0
    payload: dict = Field(default_factory=dict)


class SignalDetector(Protocol):
    name: str

    async def observe(self, segment_path: Path, meta: dict) -> list[SignalHit]: ...
