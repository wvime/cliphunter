"""YAML configuration validated with Pydantic."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ChannelConfig(BaseModel):
    """One channel the operator might authorize ClipHunter to record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    platform: Literal["youtube", "twitch", "kick"]
    handle: str | None = None
    url: str | None = None
    authorized: bool = False
    context: str = ""
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)

    @field_validator("handle", "url", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        if self.url:
            return self.url.rstrip("/").rsplit("/", 1)[-1]
        return self.platform


def channel_key(channel: ChannelConfig) -> str:
    """Stable directory name, safe on Windows."""

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

    @model_validator(mode="after")
    def _unique_channels(self) -> AppConfig:
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
