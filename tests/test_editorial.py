"""Editorial judge, boundaries, compare report, and reject reasons."""

from __future__ import annotations

import json
from pathlib import Path

from cliphunter.editorial.audio import s16le_to_floats
from cliphunter.editorial.bounds import Line, Word, clip_bounds
from cliphunter.editorial.archive import retain_clip, shifted_bounds
from cliphunter.editorial.compare import Pick, live_sim_windows, picks_from_directory, render_report, render_table
from cliphunter.editorial.compare_cmd import _without_judge
from cliphunter.editorial.cut import dead_air_trim, precise_ffmpeg_command, silence_spans
from cliphunter.editorial.feedback import suggest_editorial_threshold, summarize_feedback
from cliphunter.editorial.io import import_participants
from cliphunter.editorial.judge import ClaudeJudge, EditorialVerdict, extract_json, signal_only_verdict, verdict_from_payload
from cliphunter.editorial.names import ground_verdict
from cliphunter.editorial.score import rescale, tier_for
from cliphunter.editorial.vertical import build_ass, title_markup
from cliphunter.processing.title_overlay import TitleStyle
from cliphunter.review.service import ReviewRuntime
from cliphunter.storage.repo import SqliteRepo
from tests.test_review import _channel, _config, _pending_clip


def test_rescale_ignores_a_missing_component_and_tiers_match_the_spec():
    total, cleaned = rescale(
        {
            "event_strength": 30,
            "reaction_quality": 15,
            "cultural_relevance": 15,
            "opening_hook": 15,
            "standalone_clarity": 10,
            "meme_potential": 10,
            "audience_engagement": None,
        }
    )
    assert cleaned["audience_engagement"] is None
    assert total == 100.0
    half, _ = rescale(
        {
            "event_strength": 15,
            "reaction_quality": 7.5,
            "cultural_relevance": 7.5,
            "opening_hook": 7.5,
            "standalone_clarity": 5,
            "meme_potential": 5,
            "audience_engagement": None,
        }
    )
    assert half == 50.0
    assert tier_for(92) == "exceptional"
    assert tier_for(80) == "high"
    assert tier_for(65) == "secondary"
    assert tier_for(64) == "rejected"


def test_signal_only_does_not_invent_editorial_fields():
    verdict = signal_only_verdict(2.5)
    assert verdict.mode == "signal-only"
    assert verdict.title is None
    assert verdict.category is None
    assert verdict.participants == []
    assert verdict.why is None
    assert verdict.keep is False


def test_judge_json_is_rescaled_and_a_storyline_under_65_needs_a_name():
    payload = extract_json(
        """```json
        {"category": "humor dominicano", "participants": ["Tokischa"],
         "setup_start": 1.0, "payoff": 4.0, "end": 8.0,
         "hook": "mira la cara", "title": "Tokischa no aguantó", "sujeto": "Tokischa",
         "why": "la reacción callada es el chiste", "storyline": false, "storyline_note": null,
         "components": {"event_strength": 24, "reaction_quality": 12, "cultural_relevance": 12,
                        "opening_hook": 10, "standalone_clarity": 8, "meme_potential": 8,
                        "audience_engagement": null}}
        ```"""
    )
    verdict = verdict_from_payload(payload)
    assert verdict.mode == "editorial"
    assert verdict.category == "humor"
    assert verdict.total == round(100 * (24 + 12 + 12 + 10 + 8 + 8) / 95, 1)
    assert verdict.keep is True
    assert verdict.components["audience_engagement"] is None
    quiet = verdict_from_payload(
        {
            "category": "drama",
            "participants": ["Ana"],
            "setup_start": 0,
            "payoff": 2,
            "end": 6,
            "why": "sigue la pelea",
            "storyline": True,
            "storyline_note": "",
            "components": {"event_strength": 10, "reaction_quality": 4, "cultural_relevance": 4,
                           "opening_hook": 3, "standalone_clarity": 3, "meme_potential": 2,
                           "audience_engagement": 1},
        }
    )
    assert quiet.total < 65
    assert quiet.storyline is False
    assert quiet.keep is False
    named = quiet.model_copy(update={"storyline": True, "storyline_note": "la rivalidad con Ana"})
    from cliphunter.editorial.score import should_keep

    assert should_keep(
        55,
        storyline=True,
        storyline_note="la rivalidad con Ana",
        known_storylines=["la rivalidad con Ana"],
    )
    assert should_keep(27, storyline=True, storyline_note="la rivalidad con Ana", known_storylines=["la rivalidad con Ana"]) is False
    assert should_keep(55, storyline=True, storyline_note="la rivalidad con Ana") is False


async def test_claude_judge_uses_the_injected_completion_and_falls_back():
    async def _complete(_packet):
        return json.dumps(
            {
                "category": "chisme",
                "participants": ["Ana"],
                "setup_start": 2,
                "payoff": 5,
                "end": 9,
                "hook": "lo dijo en voz baja",
                "title": "Ana soltó el chisme",
                "sujeto": "Ana",
                "why": "la revelación se entiende sola",
                "storyline": False,
                "components": {
                    "event_strength": 27,
                    "reaction_quality": 12,
                    "cultural_relevance": 12,
                    "opening_hook": 12,
                    "standalone_clarity": 8,
                    "meme_potential": 7,
                    "audience_engagement": 4,
                },
            }
        )

    verdict = await ClaudeJudge(None, complete=_complete).judge(
        {"transcript": [{"text": "Ana soltó el chisme en voz baja"}]}
    )
    assert verdict.keep is True
    assert verdict.title == "Ana soltó el chisme"
    fallback = await ClaudeJudge(None).judge({"signal_score": 3})
    assert fallback.mode == "signal-only"
    assert fallback.title is None


def test_bounds_start_on_the_setup_sentence_and_keep_the_reaction():
    lines = [
        Line(10, 12, "te voy a decir una cosa"),
        Line(12, 14, "tú sales con él"),
        Line(14, 18, "y la cara de ella"),
    ]
    start, end, payoff = clip_bounds(lines, 12.2, 13, 16, min_seconds=15, max_seconds=60)
    assert start == 11.7
    assert end >= 18
    assert payoff == 12
    long_lines = [Line(float(i), float(i) + 1, "palabra") for i in range(0, 90)]
    start, end, _payoff = clip_bounds(long_lines, 10, 20, 80, min_seconds=15, max_seconds=60)
    assert end - start <= 60
    assert end >= 80


def test_clip_starts_at_the_first_word_not_the_pause_before_it():
    # The whisper segment opens at 10.0, but nobody speaks until 12.4.
    setup = Line(
        10.0,
        15.0,
        "pa la intimidad",
        (
            Word(12.4, 12.7, "pa"),
            Word(12.8, 13.0, "la"),
            Word(13.1, 13.8, "intimidad"),
        ),
    )
    reaction = Line(15.2, 17.0, "jajaja", (Word(15.2, 16.4, "jajaja"),))
    start, end, _payoff = clip_bounds(
        [setup, reaction],
        12.5,
        13.2,
        16.0,
        min_seconds=1,
        max_seconds=60,
        payoff_quote="intimidad",
    )
    assert start == 12.1
    assert start > 10.5
    assert end >= 16.4
    assert end - start < 20


def test_ffmpeg_trims_leading_and_trailing_silence(tmp_path: Path):
    import asyncio
    import subprocess

    from cliphunter.clip import probe_duration
    from cliphunter.editorial.cut import _trim_dead_air
    from tests.support import require_ffmpeg

    require_ffmpeg()
    source = tmp_path / "padded.mp4"
    built = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=8000:d=2.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=8000:d=2",
            "-filter_complex",
            "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr[-500:]
    before = probe_duration(source)
    assert before is not None and before > 4.5
    asyncio.run(_trim_dead_air(source))
    after = probe_duration(source)
    assert after is not None
    assert after < before - 1.5
    assert 1.2 < after < 3.2


def test_dead_air_plan_keeps_a_third_before_speech_and_drops_a_long_tail():
    log = "\n".join(
        [
            "silence_start: 0",
            "silence_end: 2.8 | silence_duration: 2.8",
            "silence_start: 8.2",
            "silence_end: 11.0 | silence_duration: 2.8",
        ]
    )
    spans = silence_spans(log)
    assert spans == [(0.0, 2.8), (8.2, 11.0)]
    window = dead_air_trim(11.0, spans)
    assert window is not None
    start, end = window
    assert start == 2.5
    assert abs(end - 8.8) < 0.01


def test_compare_report_shows_rejects_and_misses():
    old = [Pick(start=10, end=40, score=3.2, trigger="señal", accepted=True)]
    lines = [Line(100, 101.5, "jajaja pero qué fue eso")]
    accepted, rejected = _without_judge(lines, old)
    report = render_report(old, accepted, rejected=rejected)
    assert "Selector viejo" in report
    assert "10.0–40.0s" in report
    assert "solo hubo pico de señal" in report
    assert "jajaja" in report or "laughter" in report
    assert "El nuevo los encuentra" in report


def test_precise_command_reencodes_instead_of_copying():
    command = precise_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), start=1.25, duration=12)
    assert "libx264" in command
    assert "veryfast" in command
    assert "aac" in command
    assert "+faststart" in command
    assert "copy" not in command
    assert command[command.index("-c:v") + 1] == "libx264"


def test_title_markup_highlights_the_subject_and_ass_uses_real_lines():
    assert title_markup("Tokischa no aguantó", "Tokischa").startswith("*Tokischa*")
    ass = build_ass(
        title="Tokischa no aguantó",
        sujeto="Tokischa",
        lines=[Line(10, 12, "mira esa cara")],
        clip_start=10,
        style=TitleStyle(),
        duration=8,
    )
    assert "Montserrat Black" in ass
    assert "mira esa cara" in ass
    assert "Tokischa" in ass
    assert "@" not in ass


def test_pcm_scale_is_minus_one_to_one():
    import struct

    data = struct.pack("<h", 16384)
    assert s16le_to_floats(data) == [0.5]


def test_feedback_raises_the_minimum_after_bored_rejects():
    summary = summarize_feedback(
        [
            {"action": "reject", "reason": "aburrido", "category": "drama"},
            {"action": "reject", "reason": "aburrido", "category": "drama"},
            {"action": "reject", "reason": "sin_contexto", "category": "humor"},
        ],
        [{"platform": "tiktok", "retention": 0.4}],
        minimum=65,
    )
    assert summary["minimum"] == 68
    assert summary["reasons"]["aburrido"] == 2


async def test_reject_reason_is_stored(tmp_path: Path):
    config = _config(tmp_path)
    async with SqliteRepo(config.resolved_database_path()) as repo:
        channel_id = await _channel(repo)
        clip_id = await _pending_clip(repo, tmp_path, channel_id=channel_id, breakdown={"chat": 1})
        runtime = ReviewRuntime(repo=repo, config=config, env=None, enable_local=False)
        await runtime.apply_code(clip_id, "why:aburrido")
        row = await repo.get_clip(clip_id)
        assert row is not None
        assert row["status"] == "rejected"
        decision = (await repo.list_decisions(clip_id))[0]
        assert decision["reason"] == "aburrido"
        assert decision["action"] == "reject"


async def test_participant_import_keeps_rumors_unverified(tmp_path: Path):
    config = _config(tmp_path)
    path = tmp_path / "gente.json"
    path.write_text(
        json.dumps(
            [
                {
                    "nombre": "Tokischa",
                    "apodos": ["Toki"],
                    "descripcion": "artista",
                    "fuente": "https://example.com/toki",
                }
            ]
        ),
        encoding="utf-8",
    )
    async with SqliteRepo(config.resolved_database_path()) as repo:
        count = await import_participants(path, repo)
        people = await repo.list_participants()
    assert count == 1
    assert people[0]["name"] == "Tokischa"
    assert people[0]["nicknames"] == ["Toki"]
    assert people[0]["verified"] == 0
    assert people[0]["source"] == "https://example.com/toki"


def test_ungrounded_names_are_removed_from_the_title():
    verdict = EditorialVerdict(
        mode="editorial",
        title="Santiago Matias preguntó",
        hook="Santiago lo dijo",
        why="Santiago insistió",
        sujeto="Santiago Matias",
        participants=["Santiago Matias"],
        total=70,
    )
    people = [{"name": "Santiago Matias", "nicknames": ["Alofoke"]}]
    grounded = ground_verdict(verdict, "pa la intimidad, entre bomberos", people)
    assert grounded.participants == []
    assert grounded.sujeto is None
    assert "Santiago" not in (grounded.title or "")
    assert "un participante" in (grounded.title or "")


def test_topic_change_ends_the_clip_and_the_payoff_stays_inside():
    lines = [
        Line(0.0, 3.0, "pa la intimidad", (Word(0.4, 1.6, "intimidad"),)),
        Line(3.2, 5.0, "jajaja", (Word(3.2, 4.4, "jajaja"),)),
        Line(5.2, 8.0, "bienvenido Frenny"),
    ]
    start, end, _payoff = clip_bounds(
        lines,
        0.5,
        1.0,
        2.0,
        min_seconds=1,
        max_seconds=60,
        payoff_quote="pa la intimidad",
    )
    assert abs(start - 0.1) < 0.02
    assert end >= 1.6
    assert end < 5.2


def test_live_sim_windows_and_old_clip_folder(tmp_path: Path):
    windows = live_sim_windows(180, sweep=30, before=90, after=30, cooldown=45)
    assert windows[0] == (0.0, 60.0, 30.0)
    assert windows[1][2] == 90.0
    folder = tmp_path / "viejos"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"a")
    (folder / "b.mp4").write_bytes(b"b")
    picks = picks_from_directory(folder, probe=lambda _path: None)
    assert [(pick.start, pick.end) for pick in picks] == [(0.0, 30.0), (30.0, 60.0)]
    table = render_table(
        [Pick(start=12.1, end=36.0, score=61.1, category="humor", participants="Frenny", why="el remate", accepted=True)]
    )
    assert "| 12.1 | 36.0 | 61.1 | humor | Frenny | el remate | se queda |" in table


def test_calibrate_suggests_the_midpoint_of_approve_and_reject_scores():
    suggested = suggest_editorial_threshold(
        [
            {"action": "reject", "weight": 1, "breakdown_json": json.dumps({"total": 40})},
            {"action": "approve", "weight": 3, "breakdown_json": json.dumps({"total": 80})},
        ],
        65,
    )
    assert suggested == 60.0


def test_pending_clip_segments_stay_after_the_buffer_drops_them(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from cliphunter.index import Segment, SegmentIndex

    directory = tmp_path / "buffer" / "youtube_test"
    directory.mkdir(parents=True)
    origin = datetime(2026, 11, 24, tzinfo=timezone.utc)
    segments = []
    for index in range(4):
        start = origin + timedelta(seconds=index * 4)
        name = f"s{index}.ts"
        (directory / name).write_bytes(b"segment")
        segments.append(
            Segment(
                file=name,
                duration=4,
                wall_start=start.isoformat(),
                wall_end=(start + timedelta(seconds=4)).isoformat(),
            )
        )
    index = SegmentIndex(channel_key="youtube_test", platform="youtube", directory=directory, segments=segments)
    index.save()
    stored = retain_clip(
        buffer_dir=tmp_path / "buffer",
        clip_id=7,
        index_path=index.path,
        files=["s1.ts", "s2.ts"],
        margin_seconds=4,
    )
    assert stored is not None
    for segment in segments:
        (directory / segment.file).unlink()
    archived = SegmentIndex.load(stored)
    names = [segment.file for segment in archived.segments]
    assert "s1.ts" in names and "s2.ts" in names
    assert (stored.parent / "s1.ts").exists()
    start, end = shifted_bounds(segments[1].wall_start, segments[2].wall_end, "fin", 5)
    assert end > parse_end(segments[2].wall_end)


def parse_end(value: str) -> float:
    from cliphunter.timeutil import parse_iso

    return parse_iso(value).timestamp()

