from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SITE_NAME = "CineLanka"


def _strip_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = "" if quote == char else (char if not quote else quote)
            continue
        if char == "#" and not quote:
            return value[:index].strip()
    return value.strip()


def load_config_file() -> None:
    """Load local config.env only. Deployment environment variables always win."""
    for path in (BASE_DIR / "config.env", Path.cwd() / "config.env"):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            value = _strip_comment(raw_value.strip())
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1].strip()
            if key and key not in os.environ:
                os.environ[key] = value
        return


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def as_int(name: str, default: int = 0) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


load_config_file()


@dataclass(frozen=True)
class Settings:
    site_name: str
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: str
    auth_channel: str
    database_uri: str
    tmdb_api_key: str
    port: int
    public_base_url: str

    @property
    def channel_ref(self) -> int | str:
        return int(self.auth_channel) if re.fullmatch(r"-?\d+", self.auth_channel) else self.auth_channel

    @property
    def session_secret(self) -> str:
        # No extra secret is required. This is only for anonymous visitor cookies.
        seed = "|".join((self.api_hash, self.bot_token, self.owner_id, self.database_uri, "subtitle-public"))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @property
    def configured(self) -> bool:
        return bool(
            self.api_id
            and self.api_hash
            and self.bot_token
            and self.owner_id
            and self.auth_channel
            and self.database_uri
            and self.tmdb_api_key
        )


settings = Settings(
    # Used for the public website, browser titles, API label and bot headings.
    # Keep a stable default when SITE_NAME is unset or blank.
    site_name=env("SITE_NAME", DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME,
    api_id=as_int("API_ID"),
    api_hash=env("API_HASH"),
    bot_token=env("BOT_TOKEN"),
    owner_id=env("OWNER_ID"),
    auth_channel=env("AUTH_CHANNEL"),
    database_uri=env("DATABASE"),
    tmdb_api_key=env("TMDB_API"),
    port=as_int("PORT", 7360),
    # PUBLIC_BASE_URL is used only for the one public button under each channel post.
    # BASE_URL remains supported so an existing deployment does not need a rename.
    public_base_url=env("PUBLIC_BASE_URL") or env("BASE_URL"),
)
