import json

from cliphunter.storage.repo import SqliteRepo


async def _channel(repo: SqliteRepo) -> int:
    return await repo.upsert_channel(
        channel_key="youtube_tester",
        platform="youtube",
        handle="Tester",
        url=None,
        authorized=True,
        context="español",
        weights={"humor": 1.0},
        thresholds={"clip_score": 0.7},
    )


async def test_schema_roundtrip_and_two_connections(tmp_path):
    path = tmp_path / "cliphunter.sqlite"
    async with SqliteRepo(path) as left:
        channel_id = await _channel(left)
        session_id = await left.start_session(
            channel_id=channel_id,
            stream_url="https://www.youtube.com/watch?v=abcdefghijk",
            stream_title="Título",
            video_id="abcdefghijk",
            confirmed_authorized_at="2026-11-18T00:00:00+00:00",
        )
        clip_id = await left.insert_clip(
            channel_id=channel_id,
            session_id=session_id,
            kind="manual",
            status="ready",
            attribution={"streamer": "Tester", "platform": "youtube"},
            file_path=str(tmp_path / "clip.mp4"),
            duration_seconds=12.5,
        )
        await left.insert_decision(clip_id=clip_id, action="approve", adjust_seconds=0)
        await left.insert_metric(session_id=session_id, cpu_pct=10, rss_mb=80, extra={"system_cpu_pct": 12})
        async with SqliteRepo(path) as right:
            row = await right.get_channel_by_key("youtube_tester")
            assert row is not None
            assert row["context"] == "español"
            assert json.loads(row["weights_json"])["humor"] == 1.0
            session = await right.get_session(session_id)
            assert session is not None
            assert session["confirmed_authorized_at"] == "2026-11-18T00:00:00+00:00"
            assert session["status"] == "active"
            clips = await right.list_clips(channel_id)
            assert clips[0]["kind"] == "manual"
            assert json.loads(clips[0]["attribution_json"])["streamer"] == "Tester"
        changed = await left.interrupt_open_sessions()
        assert changed == 1
        session = await left.get_session(session_id)
        assert session is not None
        assert session["status"] == "interrupted"
        metrics = await left.list_metrics(session_id)
        assert metrics[0]["rss_mb"] == 80


def test_schema_file_lists_phase1_tables():
    from cliphunter.storage.repo import SCHEMA_PATH

    text = SCHEMA_PATH.read_text(encoding="utf-8")
    for table in ("channels", "sessions", "clips", "decisions", "metrics"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in text
    assert "confirmed_authorized_at" in text
    assert "signal_score" in text
    assert "breakdown_json" in text
    assert "attribution_json" in text
    assert "cpu_pct" in text
    assert "rss_mb" in text
