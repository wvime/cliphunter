import json
from pathlib import Path

import pytest

from cliphunter.clip import plan_clip
from cliphunter.index import Segment, SegmentIndex
from cliphunter.ingest import build_streamlink_command, ffmpeg_segment_command, streamlink_quality
from cliphunter.timeutil import utc_iso, utc_now
from tests.support import generate_ts, probe_duration, require_ffmpeg


def _segment(name: str, duration: float) -> Segment:
    end = utc_now()
    return Segment(
        file=name,
        duration=duration,
        wall_start=utc_iso(end),
        wall_end=utc_iso(end),
        streamer="Tester",
        platform="youtube",
        stream_url="https://www.youtube.com/watch?v=abcdefghijk",
        stream_title="Ranked",
        session_id=7,
    )


def test_plan_clip_attribution_and_overshoot(tmp_path: Path):
    directory = tmp_path / "buf"
    directory.mkdir()
    index = SegmentIndex(
        channel_key="youtube_tester",
        platform="youtube",
        directory=directory,
        session={"context": "humor en español", "streamer": "Tester"},
    )
    index.segments = [_segment("a.ts", 4), _segment("b.ts", 4), _segment("c.ts", 4)]
    for segment in index.segments:
        (directory / segment.file).write_bytes(b"x")
    plan = plan_clip(index, 6)
    assert [segment.file for segment in plan.selected] == ["b.ts", "c.ts"]
    assert plan.attribution["streamer"] == "Tester"
    assert plan.attribution["platform"] == "youtube"
    assert plan.attribution["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert plan.attribution["stream_title"] == "Ranked"
    assert plan.attribution["channel_context"] == "humor en español"
    assert plan.attribution["clip_start"]
    assert plan.attribution["clip_end"]
    assert plan.attribution["requested_seconds"] == 6
    assert plan.attribution["actual_seconds"] >= 6
    assert plan.attribution["precision"] == "keyframe-segment"
    assert plan.attribution["short_buffer"] is False
    assert plan.concat_lines[0].startswith("file '")
    assert "b.ts" in plan.concat_lines[0]


def test_plan_clip_empty_buffer(tmp_path: Path):
    index = SegmentIndex(channel_key="youtube_t", platform="youtube", directory=tmp_path)
    from cliphunter.clip import ClipError

    with pytest.raises(ClipError):
        plan_clip(index, 10)


def test_streamlink_command_prefers_low_bitrates():
    assert streamlink_quality(["480p", "720p"]) == "480p,720p,worst"
    command = build_streamlink_command("https://www.youtube.com/watch?v=abcdefghijk", ["480p", "720p"])
    assert command[0] == "streamlink"
    assert "--stdout" in command
    assert "--hls-live-edge" in command
    assert "--stream-segment-threads" in command
    assert command[command.index("--stream-segment-threads") + 1] == "1"
    assert command[command.index("--retry-max") + 1] == "2"
    assert command[-2] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert command[-1] == "480p,720p,worst"
    assert isinstance(command, list)


def test_ffmpeg_segment_command_copies():
    command = ffmpeg_segment_command(Path("seg_%05d.ts"), Path("list.csv"), 4)
    assert command[command.index("-c") + 1] == "copy"
    assert "libx264" not in command
    assert "-break_non_keyframes" in command
    assert command[command.index("-break_non_keyframes") + 1] == "0"
    assert "-segment_time" in command
    assert command[command.index("-segment_time") + 1] == "4"


async def test_assemble_real_segments_without_reencode(tmp_path: Path):
    require_ffmpeg()
    import subprocess

    source = generate_ts(tmp_path / "source.ts", seconds=12)
    directory = tmp_path / "buffer"
    directory.mkdir()
    playlist = directory / "segments.csv"
    pattern = directory / "seg_%05d.ts"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "4",
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            "-break_non_keyframes",
            "0",
            "-segment_list",
            str(playlist),
            "-segment_list_type",
            "csv",
            str(pattern),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    from cliphunter.index import parse_segment_csv

    rows = parse_segment_csv(playlist.read_text(encoding="utf-8"))
    assert len(rows) >= 2
    index = SegmentIndex(
        channel_key="youtube_tester",
        platform="youtube",
        directory=directory,
        session={"context": "test", "streamer": "Tester"},
    )
    from datetime import timedelta

    end = utc_now()
    for filename, start, stop in rows:
        duration = stop - start
        wall_end = end
        wall_start = end - timedelta(seconds=duration)
        end = wall_start
        index.add(
            Segment(
                file=Path(filename).name,
                duration=duration,
                wall_start=utc_iso(wall_start),
                wall_end=utc_iso(wall_end),
                streamer="Tester",
                platform="youtube",
                stream_url="https://www.youtube.com/watch?v=abcdefghijk",
                stream_title="Ranked",
                session_id=None,
            )
        )
    index.save()
    from cliphunter.clip import create_clip
    from cliphunter.storage.repo import SqliteRepo

    output = tmp_path / "clips" / "out.mp4"
    async with SqliteRepo(tmp_path / "cliphunter.sqlite") as repo:
        channel_id = await repo.upsert_channel(
            channel_key="youtube_tester",
            platform="youtube",
            handle="Tester",
            url=None,
            authorized=True,
            context="test",
            weights={},
            thresholds={},
        )
        result = await create_clip(
            index_path=index.path,
            seconds=6,
            output=output,
            repo=repo,
            channel_id=channel_id,
        )
        assert result.path.exists()
        assert result.path.stat().st_size > 0
        sidecar = json.loads(result.sidecar.read_text(encoding="utf-8"))
        assert sidecar["streamer"] == "Tester"
        assert sidecar["platform"] == "youtube"
        assert sidecar["stream_title"] == "Ranked"
        assert sidecar["url"].startswith("https://")
        assert sidecar["clip_start"] and sidecar["clip_end"]
        assert sidecar["clip_id"] == result.clip_id
        duration = probe_duration(result.path)
        assert 3 <= duration <= 14
        stored = await repo.list_clips(channel_id)
        assert stored[0]["kind"] == "manual"
        assert stored[0]["status"] == "ready"
        assert json.loads(stored[0]["attribution_json"])["streamer"] == "Tester"
