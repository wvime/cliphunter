"""Title overlay is configured and planned, not burned into video."""

import pytest

from cliphunter.config import AppConfig, ConfigError, load_config
from cliphunter.processing.title_overlay import PendingTitleOverlay, TitleStyle, plan_title_spans
from pathlib import Path


def test_title_style_defaults_and_example():
    bare = AppConfig(channels=[])
    assert bare.title_style.box_color == "#FFFFFF"
    assert bare.title_style.text_color == "#000000"
    assert bare.title_style.highlight_color == "#FFD400"
    assert bare.title_style.highlight_mode == "background"
    assert bare.title_style.font == "Arial"
    assert bare.title_style.renderer == "ass"
    assert bare.title_style.title_format == "*{sujeto}*"

    example = load_config(Path("config.example.yaml"))
    assert example.title_style.box_color == "#FFFFFF"
    assert example.title_style.highlight_mode == "background"
    assert "{sujeto}" in example.title_style.title_format

    alofoke = load_config(Path("config.alofoke.example.yaml"))
    assert alofoke.title_style.highlight_color == "#FFD400"
    assert alofoke.title_style.corner_radius == 16


def test_marked_subject_is_the_highlighted_span():
    spans = plan_title_spans("*{sujeto}* se cayó el mic", sujeto="Ana")
    assert [(span.text, span.highlight) for span in spans] == [
        ("Ana", True),
        (" se cayó el mic", False),
    ]
    literal = plan_title_spans("nadie esperaba a *Ibai*")
    assert [(span.text, span.highlight) for span in literal] == [
        ("nadie esperaba a ", False),
        ("Ibai", True),
    ]
    whole = plan_title_spans("{sujeto}", sujeto="Luis")
    assert [(span.text, span.highlight) for span in whole] == [("Luis", True)]


def test_title_format_must_mark_the_subject(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "title_style:\n  title_format: solo texto\n  highlight_mode: rainbow\nchannels: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)
    style = TitleStyle(highlight_mode="text", box_color="white")
    assert style.highlight_mode == "text"
    assert style.box_color == "#FFFFFF"


async def test_burn_title_is_not_implemented_yet(tmp_path: Path):
    overlay = PendingTitleOverlay()
    with pytest.raises(NotImplementedError):
        await overlay.burn_title(tmp_path / "in.mp4", tmp_path / "out.mp4", sujeto="Ana")
