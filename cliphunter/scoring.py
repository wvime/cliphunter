"""LLM scorer interface. Claude is not called in phase 1."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class ScoreResult(BaseModel):
    """What a later phase stores on the clip row (score, title, hook, hashtags)."""

    score: float
    breakdown: dict[str, float] = Field(default_factory=dict)
    title: str | None = None
    sujeto: str | None = None
    hook: str | None = None
    hashtags: list[str] = Field(default_factory=list)


class Scorer(Protocol):
    async def score(self, *, context: str, transcript: str, signals: dict) -> ScoreResult: ...


class ClaudeScorer:
    """Phase 2+. Uses ``ANTHROPIC_API_KEY``. Phase 1 only loads the key and never calls out."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def score(self, *, context: str, transcript: str, signals: dict) -> ScoreResult:
        del context, transcript, signals, self
        raise NotImplementedError("El scorer de Claude llega en una fase posterior.")
