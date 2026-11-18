import os

import psutil

from cliphunter.observability import MetricsSampler
from cliphunter.storage.repo import SqliteRepo


async def test_metrics_sample_writes_cpu_and_rss(tmp_path):
    async with SqliteRepo(tmp_path / "cliphunter.sqlite") as repo:
        channel_id = await repo.upsert_channel(
            channel_key="youtube_tester",
            platform="youtube",
            handle="Tester",
            url=None,
            authorized=True,
            context="",
            weights={},
            thresholds={},
        )
        session_id = await repo.start_session(
            channel_id=channel_id,
            stream_url="https://example.test",
            stream_title="t",
            video_id="1",
            confirmed_authorized_at="2026-11-18T00:00:00+00:00",
        )
        sampler = MetricsSampler(
            repo=repo,
            interval=30,
            streams=lambda: [
                {
                    "session_id": session_id,
                    "channel_key": "youtube_tester",
                    "pids": [os.getpid()],
                }
            ],
        )
        sampler._process.cpu_percent(None)
        psutil.cpu_percent(None)
        payload = await sampler.sample()
        assert "system_cpu_pct" in payload
        assert payload["process_rss_mb"] > 0
        assert payload["streams"][0]["rss_mb"] > 0
        rows = await repo.list_metrics(session_id)
        assert rows
        assert rows[0]["rss_mb"] > 0
        assert rows[0]["cpu_pct"] is not None
