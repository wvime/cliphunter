"""YAML configuration validated with Pydantic."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cliphunter.processing.title_overlay import TitleStyle

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/](.*)$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ConfigError(Exception):
    """The config file is missing or invalid."""


def normalize_config_path(path: Path) -> Path:
    """Keep Windows defaults usable when the same YAML is loaded on Linux.

    ``C:\\cliphunter\\buffer`` becomes ``~/cliphunter/buffer`` outside Windows.
    Paths the operator wrote for the current OS are left alone.
    """

    if os.name == "nt":
        return Path(os.path.expandvars(str(path))).expanduser()
    text = str(path).strip().strip('"').strip("'")
    match = _WIN_DRIVE.match(text)
    if match:
        parts = [part for part in re.split(r"[\\/]", match.group(1)) if part]
        return Path.home().joinpath(*parts)
    return Path(text).expanduser()


def default_buffer_dir() -> Path:
    if os.name == "nt":
        return Path(r"C:\cliphunter\buffer")
    return Path.home() / "cliphunter" / "buffer"


_CONTEXT_KEYS = ("idioma", "nicho", "buen_clip", "evitar")


class AuthorizationRecord(BaseModel):
    """Optional note of why this channel may be recorded. All fields are optional."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tipo: str | None = None
    fuente: str | None = None
    fecha: str | None = None

    @field_validator("tipo", "fuente", mode="before")
    @classmethod
    def _blank(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("fecha", mode="before")
    @classmethod
    def _fecha(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            try:
                date.fromisoformat(text)
            except ValueError as exc:
                raise ValueError("fecha must be YYYY-MM-DD") from exc
            return text
        raise ValueError("fecha must be YYYY-MM-DD")

    @model_validator(mode="after")
    def _fuente_url(self) -> AuthorizationRecord:
        if self.fuente and not re.match(r"^https?://", self.fuente):
            raise ValueError("authorization.fuente must start with http:// or https://")
        return self

    def payload(self) -> dict[str, str]:
        data: dict[str, str] = {}
        if self.tipo:
            data["tipo"] = self.tipo
        if self.fuente:
            data["fuente"] = self.fuente
        if self.fecha:
            data["fecha"] = self.fecha
        return data


def render_context(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}: {fields[key]}" for key in _CONTEXT_KEYS if fields.get(key))


class ChannelConfig(BaseModel):
    """One channel the operator might authorize ClipHunter to record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str | None = None
    credit: str | None = None
    platform: Literal["youtube", "twitch", "kick"]
    handle: str | None = None
    url: str | None = None
    authorized: bool = False
    authorization: AuthorizationRecord | None = None
    context: str = ""
    context_fields: dict[str, str] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    emotes_hype: list[str] = Field(default_factory=list)

    @field_validator("id", "handle", "url", "credit", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("emotes_hype", mode="before")
    @classmethod
    def _emotes(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise ValueError("emotes_hype must be a list")

    @model_validator(mode="before")
    @classmethod
    def _expand_context(cls, value: object) -> object:
        if not isinstance(value, dict) or not isinstance(value.get("context"), dict):
            return value
        raw = value["context"]
        unknown = sorted(set(raw) - set(_CONTEXT_KEYS))
        if unknown:
            raise ValueError("context keys not allowed: " + ", ".join(unknown))
        fields = {
            key: str(raw[key]).strip()
            for key in _CONTEXT_KEYS
            if raw.get(key) not in (None, "")
        }
        data = dict(value)
        data["context_fields"] = fields
        data["context"] = render_context(fields)
        return data

    @model_validator(mode="after")
    def _identity(self) -> ChannelConfig:
        if self.url and not re.match(r"^https?://", self.url):
            raise ValueError("url must start with http:// or https://")
        if not self.handle and not self.url:
            raise ValueError("each channel needs a handle or a url")
        return self

    @property
    def display_name(self) -> str:
        if self.handle:
            return self.handle.lstrip("@")
        if self.id:
            return self.id
        if self.url:
            return self.url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        return self.platform

    def authorization_payload(self) -> dict[str, str] | None:
        if self.authorization is None:
            return None
        payload = self.authorization.payload()
        return payload or None


def channel_key(channel: ChannelConfig) -> str:
    """Stable directory name, safe on Windows.

    An explicit ``id`` wins over the handle or URL, so ``--canal alofoke``
    matches the directory ``youtube_alofoke``.
    """

    if channel.id:
        raw = channel.id.strip()
    else:
        raw = (channel.handle or channel.url or channel.platform).strip().lstrip("@")
        raw = re.sub(r"^https?://", "", raw)
    slug = _UNSAFE.sub("_", raw).strip("._-") or "channel"
    return f"{channel.platform}_{slug[:60]}"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    max_concurrent_streams: int = Field(default=2, ge=1, le=2)
    buffer_dir: Path = Field(default_factory=default_buffer_dir)
    buffer_minutes: int = Field(default=10, ge=1, le=180)
    segment_seconds: int = Field(default=4, ge=2, le=15)
    quality: list[str] = Field(default_factory=lambda: ["480p", "720p"])
    poll_interval_seconds: int = Field(default=45, ge=15, le=300)
    skip_authorization_prompt: bool = False
    metrics_interval_seconds: int = Field(default=15, ge=5, le=300)
    log_level: str = "INFO"
    hotkey: str = "ctrl+alt+c"
    hotkey_seconds: float = Field(default=45, gt=0, le=600)
    hotkey_debounce_seconds: float = Field(default=3, ge=0, le=60)
    active_channel: str | None = None
    review_port: int = Field(default=8765, ge=1024, le=65535)
    auto_clip: bool = True
    signal_window_seconds: float = Field(default=10, gt=0, le=60)
    signal_interval_seconds: float = Field(default=2, gt=0, le=30)
    baseline_minutes: float = Field(default=10, gt=0, le=60)
    clip_min_seconds: float = Field(default=15, gt=0, le=120)
    clip_max_seconds: float = Field(default=45, gt=0, le=180)
    clip_target_seconds: float = Field(default=30, gt=0, le=180)
    clip_pre_roll_seconds: float = Field(default=8, ge=0, le=60)
    hysteresis: float = Field(default=0.65, gt=0, le=1)
    transcript_on_trigger: bool = True
    whisper_model: str = "base"
    editorial: Literal["auto", "on", "off"] = "auto"
    editorial_model: str = "claude-haiku-4-5"
    editorial_before_seconds: float = Field(default=90, ge=10, le=300)
    editorial_after_seconds: float = Field(default=30, ge=0, le=120)
    editorial_sweep_seconds: float = Field(default=30, ge=5, le=300)
    editorial_min_score: float = Field(default=65, ge=0, le=100)
    editorial_max_seconds: float = Field(default=60, ge=15, le=180)
    editorial_story_max_seconds: float = Field(default=120, ge=15, le=300)
    title_style: TitleStyle = Field(default_factory=TitleStyle)
    database_path: Path | None = None
    clips_dir: Path | None = None
    channels: list[ChannelConfig] = Field(default_factory=list)

    @field_validator("buffer_dir", "database_path", "clips_dir", mode="after")
    @classmethod
    def _normalize_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return normalize_config_path(value)

    @field_validator("quality", mode="before")
    @classmethod
    def _quality(cls, value: object) -> list[str]:
        if isinstance(value, str):
            items = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, list):
            items = [str(part).strip() for part in value if str(part).strip()]
        else:
            raise ValueError("quality must be a list or a comma-separated string")
        if not items:
            raise ValueError("quality must not be empty")
        return items

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return level

    @field_validator("hotkey")
    @classmethod
    def _hotkey(cls, value: str) -> str:
        text = value.strip().lower().replace("-", "+")
        parts = [part for part in text.split("+") if part]
        if not parts:
            raise ValueError("hotkey must not be empty")
        modifiers = {"ctrl", "control", "alt", "shift", "cmd", "win", "super"}
        if any(part in modifiers for part in parts) and parts[-1] in modifiers:
            raise ValueError("hotkey needs a non-modifier key")
        if parts[-1] in modifiers or any(part not in modifiers and part != parts[-1] for part in parts):
            raise ValueError("modifiers must come before the key, for example ctrl+alt+c")
        normalized = ["ctrl" if part == "control" else part for part in parts]
        return "+".join(normalized)

    @field_validator("active_channel", mode="before")
    @classmethod
    def _active_channel(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _unique_channels(self) -> AppConfig:
        if not (self.clip_min_seconds <= self.clip_target_seconds <= self.clip_max_seconds):
            raise ValueError("clip_min_seconds, clip_target_seconds and clip_max_seconds must be ordered")
        keys = [channel_key(channel) for channel in self.channels]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate channel keys: {', '.join(duplicates)}")
        return self

    def authorized_channels(self) -> list[ChannelConfig]:
        return [channel for channel in self.channels if channel.authorized]

    def resolved_database_path(self) -> Path:
        if self.database_path is not None:
            return self.database_path
        return self.buffer_dir.parent / "cliphunter.sqlite"

    def resolved_clips_dir(self) -> Path:
        if self.clips_dir is not None:
            return self.clips_dir
        return self.buffer_dir.parent / "clips"

    def resolved_ready_dir(self) -> Path:
        return self.buffer_dir.parent / "listos_para_subir"


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(
            f"No encuentro {path}. Copia config.example.yaml a config.yaml y edítalo."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("La configuración tiene que ser un mapa YAML.")
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"La configuración no es válida:\n{exc}") from exc
