import os
from datetime import timedelta
from pathlib import Path

from cliphunter.index import Segment, SegmentIndex, apply_retention, parse_segment_csv
from cliphunter.timeutil import parse_iso, utc_iso, utc_now


def _segment(name: str, duration: float, *, end_delta_minutes: float) -> Segment:
    end = utc_now() - timedelta(minutes=end_delta_minutes)
    start = end - timedelta(seconds=duration)
    return Segment(
        file=name,
        duration=duration,
        wall_start=utc_iso(start),
        wall_end=utc_iso(end),
        streamer="Tester",
        platform="youtube",
        stream_url="https://www.youtube.com/watch?v=abcdefghijk",
        stream_title="Hello",
    )


def test_parse_segment_csv_skips_garbage():
    text = "seg_00000.ts,0.000000,4.128000\nseg_00001.ts,4.128000,8.128000\npartial,1\n"
    rows = parse_segment_csv(text)
    assert rows == [
        ("seg_00000.ts", 0.0, 4.128),
        ("seg_00001.ts", 4.128, 8.128),
    ]


def test_select_last_uses_whole_segments():
    index = SegmentIndex(channel_key="youtube_t", platform="youtube", directory=Path("."))
    index.segments = [
        _segment("a.ts", 4, end_delta_minutes=3),
        _segment("b.ts", 4, end_delta_minutes=2),
        _segment("c.ts", 4, end_delta_minutes=1),
        _segment("d.ts", 5, end_delta_minutes=0),
    ]
    selected = index.select_last(10)
    # 5 + 4 = 9, which is still short of 10, so the third segment is included.
    assert [segment.file for segment in selected] == ["b.ts", "c.ts", "d.ts"]
    total = sum(segment.duration for segment in selected)
    assert total >= 10
    assert total - selected[0].duration < 10
    assert len(index.select_last(100)) == 4
    try:
        index.select_last(0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_retention_deletes_old_segments_and_orphans(tmp_path: Path):
    directory = tmp_path / "chan"
    directory.mkdir()
    index = SegmentIndex(channel_key="youtube_t", platform="youtube", directory=directory)
    old = _segment("old.ts", 4, end_delta_minutes=30)
    fresh = _segment("fresh.ts", 4, end_delta_minutes=1)
    index.segments = [old, fresh]
    (directory / "old.ts").write_bytes(b"old")
    (directory / "fresh.ts").write_bytes(b"fresh")
    orphan = directory / "orphan.ts"
    orphan.write_bytes(b"orphan")
    old_stamp = (utc_now() - timedelta(minutes=40)).timestamp()
    os.utime(orphan, (old_stamp, old_stamp))
    keep = directory / "writing.ts"
    keep.write_bytes(b"writing")

    removed = apply_retention(index, now=utc_now(), buffer_minutes=10)

    assert not (directory / "old.ts").exists()
    assert not orphan.exists()
    assert (directory / "fresh.ts").exists()
    assert keep.exists()
    assert [segment.file for segment in index.segments] == ["fresh.ts"]
    assert any(path.name == "old.ts" for path in removed)
    index.save()
    loaded = SegmentIndex.load(index.path)
    assert [segment.file for segment in loaded.segments] == ["fresh.ts"]
    assert loaded.segments[0].streamer == "Tester"
    assert parse_iso(loaded.segments[0].wall_end) <= utc_now()
