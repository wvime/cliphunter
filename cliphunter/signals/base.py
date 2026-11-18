"""Signal contract for automatic clipping.

``compute`` returns a raw float for one 10 s window. The engine turns that
into a per-channel z-score against a rolling 10 minute baseline. A signal
that cannot run raises ``SignalUnavailable`` and is left out of the sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class SignalHit(BaseModel):
    name: str
    score: float
    start_offset: float = 0
    end_offset: float = 0
    payload: dict = Field(default_factory=dict)


class SignalUnavailable(RuntimeError):
    """This window has no data for the signal. The rest still run."""


@dataclass
class ChatMessage:
    text: str
    t: float


@dataclass
class SignalWindow:
    channel_key: str
    start: float
    end: float
    messages: list[ChatMessage] = field(default_factory=list)
    audio_rms: float | None = None
    scene_score: float | None = None
    viewer_delta: float | None = None

    @property
    def duration(self) -> float:
        return max(0.001, self.end - self.start)


class Signal(Protocol):
    name: str

    def compute(self, window: SignalWindow) -> float: ...


class SignalDetector(Protocol):
    """Older per-segment hook. The windowed ``Signal`` is what Phase 2 runs."""

    name: str

    async def observe(self, segment_path: Path, meta: dict) -> list[SignalHit]: ...
