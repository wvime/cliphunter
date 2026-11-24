"""Pure signal functions. They only read the window."""

from __future__ import annotations

from collections import Counter

from cliphunter.signals.base import SignalUnavailable, SignalWindow

DEFAULT_EMOTES = ("😂", "🤣", "💀", "🔥", "jajaja")


class ChatVelocity:
    name = "chat_velocity"

    def compute(self, window: SignalWindow) -> float:
        return len(window.messages) / window.duration


class EmoteBurst:
    name = "emote_burst"

    def __init__(self, emotes: list[str] | tuple[str, ...] | None = None, *, repeat_at: int = 4) -> None:
        raw = list(emotes) if emotes else list(DEFAULT_EMOTES)
        self.emotes = [item.casefold() for item in raw if item.strip()]
        self.repeat_at = repeat_at

    def compute(self, window: SignalWindow) -> float:
        hits = 0
        texts: list[str] = []
        for message in window.messages:
            folded = message.text.casefold()
            texts.append(" ".join(folded.split()) or folded)
            for emote in self.emotes:
                if emote:
                    hits += folded.count(emote)
        counts = Counter(texts)
        repeats = sum(count for count in counts.values() if count >= self.repeat_at)
        return float(hits + repeats)


class AudioEnergy:
    name = "audio_energy"

    def compute(self, window: SignalWindow) -> float:
        if window.audio_rms is None:
            raise SignalUnavailable("sin audio")
        return float(window.audio_rms)


class SceneChange:
    name = "scene_change"

    def compute(self, window: SignalWindow) -> float:
        if window.scene_score is None:
            raise SignalUnavailable("sin escena")
        return float(window.scene_score)


class ViewerDelta:
    name = "viewer_delta"

    def compute(self, window: SignalWindow) -> float:
        if window.viewer_delta is None:
            raise SignalUnavailable("sin viewers")
        return float(max(window.viewer_delta, 0.0))
