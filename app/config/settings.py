"""Runtime configuration for RPMStream.

Every value is read from the environment (``.env`` is loaded automatically when
``python-dotenv`` is installed). Nothing secret is ever hard coded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any
from collections.abc import Mapping

try:  # optional dependency — the bot also runs on a plain environment
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only without dotenv
    load_dotenv = None  # type: ignore[assignment]

__all__ = ["Settings", "ConfigError"]

TRUE_VALUES = {"1", "true", "yes", "on", "y"}
FALSE_VALUES = {"0", "false", "no", "off", "n", ""}

#: Telegram only serves ``upload.getFile`` in multiples of 4 KiB, capped at 1 MiB.
MIN_CHUNK_SIZE = 64 * 1024
MAX_CHUNK_SIZE = 1024 * 1024
CHUNK_ALIGNMENT = 4096


class ConfigError(RuntimeError):
    """Raised when mandatory configuration is missing or invalid."""


def _raw(env: Mapping[str, str], key: str, default: Any = None) -> Any:
    value = env.get(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = _raw(env, key)
    if value is None:
        return default
    try:
        return int(float(str(value)))
    except ValueError as exc:  # pragma: no cover - surfaced as ConfigError below
        raise ConfigError(f"{key} must be an integer, got {value!r}") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    value = _raw(env, key)
    if value is None:
        return default
    try:
        return float(str(value))
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {value!r}") from exc


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = _raw(env, key)
    if value is None:
        return default
    text = str(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ConfigError(f"{key} must be a boolean (true/false), got {value!r}")


def _str(env: Mapping[str, str], key: str, default: str = "") -> str:
    value = _raw(env, key)
    return default if value is None else str(value)


def _opt_int(env: Mapping[str, str], key: str) -> int | None:
    value = _raw(env, key)
    if value is None:
        return None
    return _int(env, key, 0) or None


@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    # --- Telegram -------------------------------------------------------
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    session_name: str = "rpmstream"
    work_dir: str = "work"

    # --- RPMShare -------------------------------------------------------
    rpmshare_api_key: str = ""
    rpmshare_api_base: str = "https://rpmshare.com"
    rpmshare_file_url_template: str = "https://rpmshare.com/{file_code}"
    rpmshare_upload_url_override: str = ""
    rpmshare_folder_id: int | None = None
    rpmshare_category_id: int | None = None
    rpmshare_tags: str = ""
    rpmshare_public: bool = True
    rpmshare_adult: bool = False
    rpmshare_title_template: str = "{file_name}"
    rpmshare_poll_interval: float = 5.0
    rpmshare_poll_timeout: float = 180.0
    rpmshare_api_timeout: float = 60.0

    # --- Pipeline -------------------------------------------------------
    max_concurrent_uploads: int = 1
    chunk_size: int = MAX_CHUNK_SIZE
    max_retries: int = 3
    retry_base_delay: float = 3.0
    retry_max_delay: float = 60.0
    upload_timeout: float = 0.0  # 0 = no cap on the transfer itself
    max_file_size_mb: int = 0  # 0 = unlimited
    queue_max_items: int = 250
    allowed_users: tuple[int, ...] = ()
    allow_any_document: bool = False

    # --- User interface -------------------------------------------------
    progress_update_interval: float = 3.0
    progress_bar_width: int = 16
    bot_title: str = "RPMStream"
    bot_tagline: str = "Telegram → RPMShare Streaming"
    creator_name: str = "Salman Biswas"
    animated_emoji_id: str = ""  # custom animated emoji (Fragment-linked bots only)
    startup_sticker_id: str = ""
    loading_sticker_id: str = ""
    success_sticker_id: str = ""
    error_sticker_id: str = ""
    send_stage_stickers: bool = False

    # --- Logging --------------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "logs/rpmstream.log"
    log_to_file: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, dotenv_path: str | None = ".env") -> Settings:
        """Build settings from ``os.environ`` (optionally after loading ``.env``)."""
        if env is None:
            if load_dotenv is not None and dotenv_path and os.path.exists(dotenv_path):
                load_dotenv(dotenv_path, override=False)
            env = os.environ

        log_level = _str(env, "LOG_LEVEL", "INFO").upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigError(f"LOG_LEVEL must be one of CRITICAL/ERROR/WARNING/INFO/DEBUG, got {log_level!r}")

        allowed_raw = _str(env, "ALLOWED_USERS", "")
        allowed: list[int] = []
        for chunk in allowed_raw.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                allowed.append(int(chunk))
            except ValueError as exc:
                raise ConfigError(f"ALLOWED_USERS must hold numeric Telegram user ids, got {chunk!r}") from exc

        settings = cls(
            api_id=_int(env, "API_ID", 0),
            api_hash=_str(env, "API_HASH", ""),
            bot_token=_str(env, "BOT_TOKEN", ""),
            session_name=_str(env, "SESSION_NAME", "rpmstream"),
            work_dir=_str(env, "WORK_DIR", "work"),
            rpmshare_api_key=_str(env, "RPMSHARE_API_KEY", ""),
            rpmshare_api_base=_str(env, "RPMSHARE_API_BASE", "https://rpmshare.com").rstrip("/"),
            rpmshare_file_url_template=_str(env, "RPMSHARE_FILE_URL_TEMPLATE", "https://rpmshare.com/{file_code}"),
            rpmshare_upload_url_override=_str(env, "RPMSHARE_UPLOAD_URL", ""),
            rpmshare_folder_id=_opt_int(env, "RPMSHARE_FOLDER_ID"),
            rpmshare_category_id=_opt_int(env, "RPMSHARE_CATEGORY_ID"),
            rpmshare_tags=_str(env, "RPMSHARE_TAGS", ""),
            rpmshare_public=_bool(env, "RPMSHARE_FILE_PUBLIC", True),
            rpmshare_adult=_bool(env, "RPMSHARE_FILE_ADULT", False),
            rpmshare_title_template=_str(env, "RPMSHARE_TITLE_TEMPLATE", "{file_name}"),
            rpmshare_poll_interval=_float(env, "RPMSHARE_POLL_INTERVAL", 5.0),
            rpmshare_poll_timeout=_float(env, "RPMSHARE_POLL_TIMEOUT", 180.0),
            rpmshare_api_timeout=_float(env, "RPMSHARE_API_TIMEOUT", 60.0),
            max_concurrent_uploads=max(1, _int(env, "MAX_CONCURRENT_UPLOADS", 1)),
            chunk_size=_int(env, "CHUNK_SIZE", MAX_CHUNK_SIZE),
            max_retries=max(0, _int(env, "MAX_RETRIES", 3)),
            retry_base_delay=_float(env, "RETRY_BASE_DELAY", 3.0),
            retry_max_delay=_float(env, "RETRY_MAX_DELAY", 60.0),
            upload_timeout=_float(env, "UPLOAD_TIMEOUT", 0.0),
            max_file_size_mb=max(0, _int(env, "MAX_FILE_SIZE_MB", 0)),
            queue_max_items=max(1, _int(env, "QUEUE_MAX_ITEMS", 250)),
            allowed_users=tuple(allowed),
            allow_any_document=_bool(env, "ALLOW_ANY_DOCUMENT", False),
            progress_update_interval=_float(env, "PROGRESS_UPDATE_INTERVAL", 3.0),
            progress_bar_width=max(6, _int(env, "PROGRESS_BAR_WIDTH", 16)),
            bot_title=_str(env, "BOT_TITLE", "RPMStream"),
            bot_tagline=_str(env, "BOT_TAGLINE", "Telegram → RPMShare Streaming"),
            creator_name=_str(env, "CREATOR_NAME", "Salman Biswas"),
            animated_emoji_id=_str(env, "ANIMATED_EMOJI_ID", ""),
            startup_sticker_id=_str(env, "STARTUP_STICKER_ID", ""),
            loading_sticker_id=_str(env, "LOADING_STICKER_ID", ""),
            success_sticker_id=_str(env, "SUCCESS_STICKER_ID", ""),
            error_sticker_id=_str(env, "ERROR_STICKER_ID", ""),
            send_stage_stickers=_bool(env, "SEND_STAGE_STICKERS", False),
            log_level=log_level,
            log_file=_str(env, "LOG_FILE", "logs/rpmstream.log"),
            log_to_file=_bool(env, "LOG_TO_FILE", True),
        )
        settings.validate()
        return settings

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`ConfigError` with *all* problems found at once."""
        missing = [
            name
            for name, value in (
                ("API_ID", self.api_id),
                ("API_HASH", self.api_hash),
                ("BOT_TOKEN", self.bot_token),
                ("RPMSHARE_API_KEY", self.rpmshare_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing) + ". Copy .env.example to .env and fill them in."
            )
        if not self.rpmshare_api_base.startswith(("http://", "https://")):
            raise ConfigError("RPMSHARE_API_BASE must start with http:// or https://")
        if "{file_code}" not in self.rpmshare_file_url_template:
            raise ConfigError("RPMSHARE_FILE_URL_TEMPLATE must contain the {file_code} placeholder")
        if self.progress_update_interval < 1.0:
            raise ConfigError("PROGRESS_UPDATE_INTERVAL must be at least 1.0 second to respect Telegram flood limits")
        if self.retry_base_delay <= 0 or self.retry_max_delay < self.retry_base_delay:
            raise ConfigError("Retry delays must satisfy 0 < RETRY_BASE_DELAY <= RETRY_MAX_DELAY")
        object.__setattr__(self, "chunk_size", self._normalise_chunk_size(self.chunk_size))

    @staticmethod
    def _normalise_chunk_size(value: int) -> int:
        """Clamp the chunk size into Telegram's accepted window, 4 KiB aligned."""
        value = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, int(value)))
        remainder = value % CHUNK_ALIGNMENT
        if remainder:
            value -= remainder
        return max(MIN_CHUNK_SIZE, value)

    # ------------------------------------------------------------------
    def user_allowed(self, user_id: int | None) -> bool:
        """``True`` when the bot is public or the user is on the allow list."""
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def safe_repr(self) -> str:
        """A redacted, single line summary used at startup (never logs secrets)."""
        secrets = {"api_hash": self.api_hash, "bot_token": self.bot_token, "rpmshare_api_key": self.rpmshare_api_key}
        shown = {
            f.name: ("***" if secrets.get(f.name) else "unset") if f.name in secrets else getattr(self, f.name)
            for f in fields(self)
        }
        shown["api_id"] = str(self.api_id) if self.api_id else "unset"
        return " ".join(f"{k}={v!r}" for k, v in shown.items())
