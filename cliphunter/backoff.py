"""Exponential backoff with a cap. Used when a live ingest drops."""

from __future__ import annotations

import random
import time
from collections.abc import Callable


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: float = 0.0,
    rng: Callable[[], float] | None = None,
) -> float:
    """Return the delay for ``attempt`` (0-based).

    The sequence without jitter is base, 2*base, 4*base, ... until ``cap``.
    Jitter multiplies that value by a factor in ``[1-jitter, 1+jitter]``.
    """

    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if base < 0 or cap < 0:
        raise ValueError("base and cap must be >= 0")
    raw = min(cap, base * (2**attempt))
    if jitter <= 0 or rng is None:
        return raw
    swing = (rng() * 2.0 - 1.0) * jitter
    return max(0.0, raw * (1.0 + swing))


class ExponentialBackoff:
    """Stateful helper. A healthy attempt (long enough) resets the counter."""

    def __init__(
        self,
        *,
        base: float = 1.0,
        cap: float = 60.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        healthy_after: float = 30.0,
    ) -> None:
        self.base = base
        self.cap = cap
        self.jitter = jitter
        self.rng = rng
        self.clock = clock
        self.healthy_after = healthy_after
        self.attempt = 0
        self._started: float | None = None

    def next_delay(self) -> float:
        delay = backoff_delay(
            self.attempt,
            base=self.base,
            cap=self.cap,
            jitter=self.jitter,
            rng=self.rng,
        )
        self.attempt += 1
        return delay

    def mark_attempt_started(self) -> None:
        self._started = self.clock()

    def mark_attempt_finished(self) -> None:
        if self._started is not None and (self.clock() - self._started) >= self.healthy_after:
            self.attempt = 0
        self._started = None

    def reset(self) -> None:
        self.attempt = 0
        self._started = None
