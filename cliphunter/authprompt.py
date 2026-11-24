"""Console confirmation before any recording starts."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

from cliphunter.config import ChannelConfig, channel_key
from cliphunter.jsonlog import log_event
from cliphunter.timeutil import utc_iso

AUTH_QUESTION = "¿confirmas que este canal está autorizado?"
_YES = {"s", "si", "sí", "y", "yes"}
_SKIP_ENV = {"1", "true", "yes", "si", "sí"}


class AuthorizationRequired(RuntimeError):
    """Non-interactive launch without an explicit skip flag."""


def resolve_prompt_mode(
    *,
    skip_flag: bool,
    config_flag: bool,
    env: Mapping[str, str] | None = None,
    is_tty: bool | None = None,
) -> str:
    """Return ``skip``, ``prompt``, or ``refuse``."""

    source = os.environ if env is None else env
    raw = str(source.get("CLIPHUNTER_SKIP_AUTH_PROMPT", "")).strip().lower()
    if skip_flag or config_flag or raw in _SKIP_ENV:
        return "skip"
    tty = sys.stdin.isatty() if is_tty is None else is_tty
    if not tty:
        return "refuse"
    return "prompt"


def confirm_authorized(
    channel_label: str,
    *,
    input_fn: Callable[[str], str] = input,
) -> bool:
    prompt = f"{AUTH_QUESTION} [{channel_label}] (s/n): "
    try:
        answer = input_fn(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in _YES


def collect_confirmations(
    channels: list[ChannelConfig],
    mode: str,
    *,
    input_fn: Callable[[str], str] = input,
) -> dict[str, str]:
    """Map channel key -> ISO timestamp for channels this process may record.

    Asked once per channel at process start, before the watcher runs.
    A later live session reuses that timestamp in ``confirmed_authorized_at``.
    """

    if mode == "refuse" and channels:
        raise AuthorizationRequired(
            "Esta ejecución no es interactiva. Pasa --yes, o pon "
            "skip_authorization_prompt: true, o CLIPHUNTER_SKIP_AUTH_PROMPT=1 "
            "para confirmar los canales con authorized: true sin preguntar."
        )
    confirmed: dict[str, str] = {}
    for channel in channels:
        key = channel_key(channel)
        label = f"{channel.platform}:{channel.display_name}"
        if mode == "skip":
            confirmed[key] = utc_iso()
            log_event("INFO", "auth.skipped", channel=key, label=label)
            continue
        if confirm_authorized(label, input_fn=input_fn):
            confirmed[key] = utc_iso()
            log_event("INFO", "auth.confirmed", channel=key, label=label)
        else:
            log_event("WARNING", "auth.declined", channel=key, label=label)
    return confirmed
