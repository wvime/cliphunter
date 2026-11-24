"""Per-signal fixtures and the scorer that turns them into a clip window."""

from __future__ import annotations

from cliphunter.signals.aggregate import (
    MomentTracker,
    canonical_weights,
    choose_bounds,
    combine,
    segments_for_window,
)
from cliphunter.signals.base import ChatMessage, SignalUnavailable, SignalWindow
from cliphunter.signals.baseline import RollingBaseline
from cliphunter.signals.features import AudioEnergy, ChatVelocity, EmoteBurst, SceneChange, ViewerDelta
from cliphunter.signals.media import rms_from_s16le, scene_scores_from_text
from cliphunter.signals.nudge import ChannelNudge
from cliphunter.signals.youtube_chat import messages_from_payload
from cliphunter.calibrate import suggest
from cliphunter.index import Segment, SegmentIndex
from cliphunter.replay import load_chat, replay_moments


def _window(**kwargs) -> SignalWindow:
    data = {"channel_key": "youtube_test", "start": 0.0, "end": 10.0}
    data.update(kwargs)
    return SignalWindow(**data)


def test_chat_velocity_and_emote_repetition():
    quiet = _window(messages=[])
    assert ChatVelocity().compute(quiet) == 0
    burst = [
        ChatMessage("JAJAJA 😂", 1),
        ChatMessage("JAJAJA 😂", 2),
        ChatMessage("jajaja 🤣", 3),
        ChatMessage("vamos", 4),
        ChatMessage("vamos", 5),
        ChatMessage("vamos", 6),
        ChatMessage("vamos", 7),
    ]
    window = _window(messages=burst)
    assert ChatVelocity().compute(window) == 0.7
    emotes = EmoteBurst(["😂", "🤣", "💀", "🔥", "JAJAJA", "jajaja"])
    # 3 emotes + 3 jajaja-forms counted inside the text, plus 4 identical "vamos".
    assert emotes.compute(window) >= 7


def test_audio_scene_and_viewers_degrade_cleanly():
    empty = _window()
    for signal in (AudioEnergy(), SceneChange(), ViewerDelta()):
        try:
            signal.compute(empty)
        except SignalUnavailable:
            pass
        else:
            raise AssertionError(signal.name)
    assert AudioEnergy().compute(_window(audio_rms=0.2)) == 0.2
    assert SceneChange().compute(_window(scene_score=0.4)) == 0.4
    assert ViewerDelta().compute(_window(viewer_delta=-30)) == 0
    assert ViewerDelta().compute(_window(viewer_delta=12)) == 12


def test_pcm_rms_and_scene_parser():
    import struct

    silence = struct.pack("<4h", 0, 0, 0, 0)
    assert rms_from_s16le(silence) == 0
    loud = struct.pack("<2h", 32767, -32768)
    assert rms_from_s16le(loud) > 0.9
    text = "frame:0 pts:0\nlavfi.scene_score=0.010\nlavfi.scene_score=0.42\n"
    assert scene_scores_from_text(text) == 0.42
    assert scene_scores_from_text("nada") == 0


def test_baseline_is_zero_until_ready_then_spikes_off_a_flat_line():
    baseline = RollingBaseline(horizon=600, min_samples=5)
    for t in range(5):
        assert baseline.add(t, 1.0) == 0
    assert baseline.add(5, 1.0) == 0
    assert baseline.add(6, 9.0) == 3.0


def test_combine_drops_a_failed_signal_and_renormalizes():
    tick = combine(
        {
            "chat_velocity": {"raw": 2, "z": 3.0, "weight": 0.35},
            "emote_burst": {"raw": 4, "z": 3.0, "weight": 0.25},
        },
        ["audio_energy", "scene_change", "viewer_delta"],
    )
    assert tick.score == 3.0
    assert "audio_energy" in tick.degraded
    assert canonical_weights({"chat": 0.5, "audio": 0.5})["chat_velocity"] == 0.5


def test_hysteresis_and_clip_bounds_stay_inside_15_to_45():
    tracker = MomentTracker()
    assert tracker.push(0, 1.0, threshold=2.5, release=1.6, max_len=45) == "idle"
    assert tracker.push(2, 3.0, threshold=2.5, release=1.6, max_len=45) == "rising"
    assert tracker.push(4, 1.0, threshold=2.5, release=1.6, max_len=45) == "fire"
    start, end, peak = choose_bounds(
        [(2, 3.0), (4, 1.0)],
        min_seconds=15,
        max_seconds=45,
        target_seconds=30,
        pre_roll=8,
    )
    assert peak == 2
    assert 15 <= end - start <= 45

    long = [(float(t), 3.0) for t in range(0, 50, 2)]
    start, end, _peak = choose_bounds(
        long, min_seconds=15, max_seconds=45, target_seconds=30, pre_roll=8
    )
    assert end - start <= 45


def test_segments_cover_the_peak_and_respect_min_max(tmp_path):
    from datetime import datetime, timedelta, timezone

    directory = tmp_path / "buffer"
    directory.mkdir()
    (directory / "a.ts").write_bytes(b"x")
    origin = datetime(2026, 11, 18, tzinfo=timezone.utc)
    segments = []
    for index in range(12):
        start = origin + timedelta(seconds=index * 4)
        segments.append(
            Segment(
                file="a.ts",
                duration=4,
                wall_start=start.isoformat(),
                wall_end=(start + timedelta(seconds=4)).isoformat(),
            )
        )
    index = SegmentIndex(channel_key="youtube_test", platform="youtube", directory=directory, segments=segments)
    peak = origin.timestamp() + 20
    chosen = segments_for_window(index, peak - 8, peak + 22, peak, min_seconds=15, max_seconds=45)
    total = sum(segment.duration for segment in chosen)
    assert 15 <= total <= 45
    assert any(abs(((datetime.fromisoformat(segment.wall_start).timestamp() + datetime.fromisoformat(segment.wall_end).timestamp()) / 2) - peak) < 8 for segment in chosen)


def test_youtube_chat_payload_reads_text_emoji_and_continuation():
    payload = {
        "continuationContents": {
            "liveChatContinuation": {
                "continuations": [
                    {"invalidationContinuationData": {"continuation": "NEXT_TOKEN_123456"}}
                ],
                "actions": [
                    {
                        "addChatItemAction": {
                            "item": {
                                "liveChatTextMessageRenderer": {
                                    "timestampUsec": "1700000000000000",
                                    "message": {
                                        "runs": [
                                            {"text": "JAJAJA "},
                                            {"emoji": {"shortcuts": ["😂"]}},
                                        ]
                                    },
                                }
                            }
                        }
                    }
                ],
            }
        }
    }
    messages, continuation = messages_from_payload(payload, now=10)
    assert continuation == "NEXT_TOKEN_123456"
    assert len(messages) == 1
    assert "JAJAJA" in messages[0].text
    assert "😂" in messages[0].text
    assert messages[0].t == 1700000000


def test_nudge_moves_a_little_and_stays_inside_bounds():
    nudge = ChannelNudge(threshold=2.5, floor=1.25, ceiling=5.0)
    assert nudge.observe("reject") is False
    assert nudge.observe("reject") is False
    assert nudge.observe("reject") is True
    assert nudge.threshold == 2.7
    assert nudge.observe("approve") is False
    assert nudge.observe("approve") is True
    assert nudge.threshold == 2.565
    capped = ChannelNudge(threshold=5.0, floor=1.25, ceiling=5.0)
    capped.consecutive_rejections = 2
    assert capped.observe("reject") is False
    assert capped.threshold == 5.0


def test_calibrate_weights_hotkey_approvals_more_than_a_plain_reject():
    suggestion = suggest(
        [
            {"action": "reject", "weight": 1, "score": 2.0, "signals": {"chat_velocity": 0.5, "audio_energy": 0.5}},
            {"action": "approve", "weight": 3, "score": 4.0, "signals": {"chat_velocity": 3.0, "audio_energy": 0.2}},
        ],
        threshold=2.5,
        weights=canonical_weights(None),
    )
    assert suggestion["threshold"] == 3.0
    assert suggestion["weights"]["chat_velocity"] > suggestion["weights"]["audio_energy"]


def test_replay_finds_a_chat_burst_and_keeps_the_clip_in_range(tmp_path):
    chat = []
    for t in range(0, 20, 2):
        chat.append({"t": t, "text": "hola"})
    for t in (20, 21, 22):
        chat.extend({"offset": t, "message": "JAJAJA 😂"} for _ in range(8))
    path = tmp_path / "chat.json"
    path.write_text(__import__("json").dumps(chat), encoding="utf-8")
    messages = load_chat(path)
    moments = replay_moments(messages, duration=40, min_samples=5, threshold=2.5)
    assert moments
    moment = moments[0]
    assert 15 <= moment["end"] - moment["start"] <= 45
    assert moment["score"] >= 2.5
    assert "chat_velocity" in moment["breakdown"]["signals"]
