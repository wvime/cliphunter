import pytest

from cliphunter.authprompt import (
    AUTH_QUESTION,
    AuthorizationRequired,
    collect_confirmations,
    confirm_authorized,
    resolve_prompt_mode,
)
from cliphunter.config import AppConfig, ChannelConfig, channel_key


def _channel() -> ChannelConfig:
    return ChannelConfig(platform="youtube", handle="@Canal", authorized=True, context="es")


def test_confirm_accepts_spanish_and_english():
    assert confirm_authorized("youtube:Canal", input_fn=lambda _prompt: "sí")
    assert confirm_authorized("youtube:Canal", input_fn=lambda _prompt: " S ")
    assert confirm_authorized("youtube:Canal", input_fn=lambda _prompt: "yes")
    assert not confirm_authorized("youtube:Canal", input_fn=lambda _prompt: "n")
    assert not confirm_authorized("youtube:Canal", input_fn=lambda _prompt: "")


def test_prompt_contains_the_required_question():
    seen: list[str] = []

    def _input(prompt: str) -> str:
        seen.append(prompt)
        return "s"

    assert confirm_authorized("youtube:Canal", input_fn=_input)
    assert seen[0].startswith(AUTH_QUESTION)
    assert "[youtube:Canal]" in seen[0]


def test_collect_skip_does_not_ask():
    def _boom(_prompt: str) -> str:
        raise AssertionError("no debería preguntar")

    confirmed = collect_confirmations([_channel()], "skip", input_fn=_boom)
    assert channel_key(_channel()) in confirmed


def test_collect_decline_and_refuse():
    declined = collect_confirmations([_channel()], "prompt", input_fn=lambda _prompt: "no")
    assert declined == {}
    with pytest.raises(AuthorizationRequired):
        collect_confirmations([_channel()], "refuse")


def test_resolve_prompt_mode():
    assert resolve_prompt_mode(skip_flag=True, config_flag=False, env={}, is_tty=False) == "skip"
    assert resolve_prompt_mode(skip_flag=False, config_flag=True, env={}, is_tty=False) == "skip"
    assert (
        resolve_prompt_mode(
            skip_flag=False,
            config_flag=False,
            env={"CLIPHUNTER_SKIP_AUTH_PROMPT": "1"},
            is_tty=False,
        )
        == "skip"
    )
    assert resolve_prompt_mode(skip_flag=False, config_flag=False, env={}, is_tty=False) == "refuse"
    assert resolve_prompt_mode(skip_flag=False, config_flag=False, env={}, is_tty=True) == "prompt"


def test_prepare_run_skip_and_empty(tmp_path):
    from cliphunter.app import prepare_run
    from cliphunter.authprompt import AuthorizationRequired as AppAuth

    config = AppConfig(
        buffer_dir=tmp_path / "buffer",
        channels=[_channel()],
    )
    plan = prepare_run(config, skip_flag=True)
    assert channel_key(_channel()) in plan.confirmations
    assert plan.channels[0].handle == "@Canal"

    empty = AppConfig(buffer_dir=tmp_path / "buffer", channels=[])
    with pytest.raises(AppAuth):
        prepare_run(empty, skip_flag=True)
