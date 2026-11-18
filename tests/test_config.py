from pathlib import Path

import pytest

from cliphunter.config import (
    AppConfig,
    ChannelConfig,
    ConfigError,
    channel_key,
    load_config,
    normalize_config_path,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_and_authorized_filter(tmp_path: Path):
    path = _write(
        tmp_path,
        """
channels:
  - platform: youtube
    handle: "@Si"
    authorized: true
  - platform: twitch
    handle: "no"
    authorized: false
""",
    )
    config = load_config(path)
    assert config.max_concurrent_streams == 2
    assert config.buffer_minutes == 10
    assert config.segment_seconds == 4
    assert config.quality == ["480p", "720p"]
    assert config.poll_interval_seconds == 45
    names = [channel.display_name for channel in config.authorized_channels()]
    assert names == ["Si"]


def test_quality_comma_string_and_minimal_yaml(tmp_path: Path):
    path = _write(
        tmp_path,
        """
quality: "480p, 720p"
channels: []
""",
    )
    config = load_config(path)
    assert config.quality == ["480p", "720p"]
    assert config.authorized_channels() == []


def test_rejects_unknown_field_bad_platform_and_missing_identity(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "channels: []\nnope: 1\n"))
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: vimeo
    handle: abc
""",
            )
        )
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: youtube
    authorized: true
""",
            )
        )


def test_rejects_too_many_streams_and_bad_url(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "max_concurrent_streams: 3\nchannels: []\n"))
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: kick
    url: kick.com/foo
""",
            )
        )


def test_duplicate_channel_keys(tmp_path: Path):
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: youtube
    handle: "@Mismo"
  - platform: youtube
    handle: "Mismo"
""",
            )
        )


def test_channel_key_is_filesystem_safe():
    channel = ChannelConfig(platform="youtube", handle="@Ibai Llanos", url="https://youtu.be/x")
    assert channel_key(channel) == "youtube_Ibai_Llanos"
    assert channel.display_name == "Ibai Llanos"


def test_example_config_loads():
    config = load_config(Path("config.example.yaml"))
    assert config.max_concurrent_streams == 2
    assert {channel.platform for channel in config.channels} == {"youtube", "twitch", "kick"}
    assert config.authorized_channels() == []
    assert config.buffer_minutes == 10
    assert config.segment_seconds == 4


def test_windows_buffer_path_is_remapped_off_windows():
    remapped = normalize_config_path(Path(r"C:\cliphunter\buffer"))
    if __import__("os").name == "nt":
        assert "cliphunter" in str(remapped).lower()
    else:
        assert remapped == Path.home() / "cliphunter" / "buffer"


def test_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "no-such.yaml")


def test_direct_model_defaults(tmp_path: Path):
    config = AppConfig(buffer_dir=tmp_path, channels=[])
    assert config.resolved_database_path() == tmp_path.parent / "cliphunter.sqlite"
    assert config.resolved_clips_dir() == tmp_path.parent / "clips"
