from pathlib import Path

import pytest

from cliphunter.calibrate import calibrate_command
from cliphunter.processing import ClipProcessor
from cliphunter.replay import replay_command
from cliphunter.review import Decision
from cliphunter.scoring import ClaudeScorer
from cliphunter.signals import SignalHit


def test_env_example_has_empty_secrets_and_gitignore_covers_env():
    ignored = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in ignored
    assert "config.yaml" in ignored
    example = Path(".env.example").read_text(encoding="utf-8")
    for name in ("YOUTUBE_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "ANTHROPIC_API_KEY"):
        assert name in example
    for line in example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        assert stripped.split("=", 1)[1].strip() == ""


def test_later_phases_are_explicitly_unimplemented():
    with pytest.raises(NotImplementedError):
        replay_command()
    with pytest.raises(NotImplementedError):
        calibrate_command()

    async def _score() -> None:
        await ClaudeScorer(api_key=None).score(context="", transcript="", signals={})

    with pytest.raises(NotImplementedError):
        import asyncio

        asyncio.run(_score())
    SignalHit(name="laugh", score=0.5)
    Decision(action="adjust", adjust_seconds=5)
    assert ClipProcessor is not None
