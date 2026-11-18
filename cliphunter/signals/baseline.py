"""Rolling baseline. A flat history treats any jump as a spike."""

from __future__ import annotations

import math
from collections import deque


class RollingBaseline:
    def __init__(self, *, horizon: float = 600.0, min_samples: int = 5) -> None:
        self.horizon = horizon
        self.min_samples = min_samples
        self.samples: deque[tuple[float, float]] = deque()

    def add(self, t: float, value: float) -> float:
        """Z-score ``value`` against history, then store it."""

        self._trim(t)
        score = self.zscore(value)
        self.samples.append((t, value))
        return score

    def zscore(self, value: float) -> float:
        if len(self.samples) < self.min_samples:
            return 0.0
        values = [sample for _, sample in self.samples]
        mean = sum(values) / len(values)
        variance = sum((sample - mean) ** 2 for sample in values) / len(values)
        std = math.sqrt(variance)
        if std < 1e-6:
            return 0.0 if value <= mean + 1e-9 else 3.0
        return (value - mean) / std

    def _trim(self, t: float) -> None:
        cutoff = t - self.horizon
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
