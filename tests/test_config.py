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


def test_alofoke_example_authorization_reaches_clip_attribution(tmp_path: Path):
    from cliphunter.clip import find_channel, plan_clip
    from cliphunter.index import Segment, SegmentIndex
    from cliphunter.timeutil import utc_iso, utc_now

    config = load_config(Path("config.alofoke.example.yaml"))
    assert config.max_concurrent_streams == 2
    assert config.buffer_minutes == 10
    assert config.segment_seconds == 4
    assert config.quality == ["480p", "720p"]
    assert config.poll_interval_seconds == 45
    assert len(config.channels) == 1
    channel = find_channel(config, "alofoke")
    assert channel.authorized is True
    assert channel.platform == "youtube"
    assert channel.url == "https://www.youtube.com/@Alofokeradioshow"
    assert channel_key(channel) == "youtube_alofoke"
    assert channel.display_name == "alofoke"
    assert channel.authorization_payload() == {
        "tipo": "permiso público del creador",
        "fuente": "https://www.instagram.com/p/DekQ6nYKdhy/",
        "fecha": "2026-11-17",
    }
    assert channel.context_fields["idioma"] == "es"
    assert channel.context_fields["nicho"] == "radio show / entretenimiento, reality TV"
    assert "momentos virales" in channel.context_fields["buen_clip"]
    assert "violencia grave" in channel.context_fields["evitar"]
    assert "idioma: es" in channel.context
    assert channel.weights == {
        "chat": 0.35,
        "emotes": 0.25,
        "audio": 0.30,
        "scene": 0.05,
        "viewers": 0.05,
    }
    assert channel.thresholds["signal_score"] == 2.5
    assert channel.thresholds["cooldown_s"] == 45
    assert channel.emotes_hype == ["😂", "🤣", "💀", "🔥", "JAJAJA", "jajaja"]

    directory = tmp_path / "buffer"
    directory.mkdir()
    (directory / "a.ts").write_bytes(b"segment")
    now = utc_now()
    index = SegmentIndex(
        channel_key=channel_key(channel),
        platform="youtube",
        directory=directory,
        session={
            "streamer": channel.display_name,
            "context": channel.context,
            "authorization": channel.authorization_payload(),
        },
    )
    index.add(
        Segment(
            file="a.ts",
            duration=4,
            wall_start=utc_iso(now),
            wall_end=utc_iso(now),
            streamer="alofoke",
            platform="youtube",
            stream_url=channel.url,
            stream_title="Show",
        )
    )
    plan = plan_clip(index, 4)
    assert plan.attribution["authorization"] == channel.authorization_payload()
    assert plan.attribution["streamer"] == "alofoke"
    assert "buen_clip:" in plan.attribution["channel_context"]


def test_authorization_fields_are_optional(tmp_path: Path):
    config = load_config(
        _write(
            tmp_path,
            """
channels:
  - platform: youtube
    handle: "@SoloTipo"
    authorization:
      tipo: "nota interna"
""",
        )
    )
    assert config.channels[0].authorization_payload() == {"tipo": "nota interna"}
    bare = load_config(
        _write(
            tmp_path,
            """
channels:
  - platform: youtube
    handle: "@SinPermiso"
""",
        )
    )
    assert bare.channels[0].authorization is None
    assert bare.channels[0].authorization_payload() is None


def test_authorization_fecha_and_context_are_validated(tmp_path: Path):
    with pytest.raises(ConfigError, match="fecha"):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: youtube
    handle: "@Fecha"
    authorization:
      fecha: ayer
""",
            )
        )
    with pytest.raises(ConfigError, match="context keys"):
        load_config(
            _write(
                tmp_path,
                """
channels:
  - platform: youtube
    handle: "@Ctx"
    context:
      idioma: es
      desconocido: no
""",
            )
        )


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
