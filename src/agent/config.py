from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class OllamaSettings(BaseModel):
    host: str = "http://localhost:11434"
    filter_model: str = "qwen2.5:3b"
    personalize_model: str = "llama3.1:8b"
    parse_model: str = "qwen2.5:3b"
    request_timeout_seconds: int = 120


class CapsSettings(BaseModel):
    profile_visits_per_day: int = 25
    linkedin_messages_per_day: int = 15
    emails_per_day: int = 40


class DelayWindow(BaseModel):
    mean_seconds: float
    stdev_seconds: float
    min_seconds: float


class WorkingHours(BaseModel):
    start: time
    end: time
    tz: str = "UTC"
    enforce: bool = True

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


class DelaysSettings(BaseModel):
    between_profile_visits: DelayWindow
    between_messages: DelayWindow
    working_hours: WorkingHours


class SearchSettings(BaseModel):
    prefer_sales_navigator: bool = True
    max_results_per_query: int = 100


class LinkedInSettings(BaseModel):
    browser_profile_dir: Path
    headed: bool = True
    slow_mo_ms: int = 50


class EmailSettings(BaseModel):
    from_address: EmailStr
    from_name: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    signature_file: Path
    reply_to: str = ""


class TemplatesSettings(BaseModel):
    linkedin_message: Path
    email_subject: Path
    email_body: Path


class FilterSettings(BaseModel):
    criteria: str
    min_score: int = 6


class PathsSettings(BaseModel):
    database: Path
    audit_log: Path
    app_log: Path


class _Secrets(BaseSettings):
    """Secrets pulled from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    gmail_app_password: str = ""
    gmail_from_address: str = ""


class Settings(BaseModel):
    ollama: OllamaSettings
    caps: CapsSettings
    delays: DelaysSettings
    search: SearchSettings
    linkedin: LinkedInSettings
    email: EmailSettings
    templates: TemplatesSettings
    filter: FilterSettings
    paths: PathsSettings

    secrets: _Secrets = Field(default_factory=_Secrets)

    @field_validator("ollama", "caps", "delays", "search", "linkedin", "email",
                     "templates", "filter", "paths", mode="before")
    @classmethod
    def _coerce_dict(cls, v):  # noqa: ANN001
        return v or {}

    def resolve_path(self, p: Path) -> Path:
        """Resolve config-relative paths against the project root."""
        p = Path(p)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    def ensure_dirs(self) -> None:
        for p in (
            self.resolve_path(self.paths.database).parent,
            self.resolve_path(self.paths.audit_log).parent,
            self.resolve_path(self.paths.app_log).parent,
            self.resolve_path(self.linkedin.browser_profile_dir),
        ):
            p.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Run `agent init` to scaffold one."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load_settings(config_path: Path | None = None) -> Settings:
    """Load and cache settings from config.yaml + .env."""
    load_dotenv(DEFAULT_ENV_PATH, override=False)
    raw = _load_yaml(config_path or DEFAULT_CONFIG_PATH)
    settings = Settings(**raw)
    settings.ensure_dirs()
    return settings


__all__ = [
    "Settings",
    "OllamaSettings",
    "CapsSettings",
    "DelaysSettings",
    "DelayWindow",
    "WorkingHours",
    "SearchSettings",
    "LinkedInSettings",
    "EmailSettings",
    "TemplatesSettings",
    "FilterSettings",
    "PathsSettings",
    "PROJECT_ROOT",
    "load_settings",
]
