"""Configuration management for the price monitoring service."""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        parse_env_var="_parse_env_var",
    )

    database_url: str = Field(
        "postgresql+psycopg2://price_user:price_pass@db:5432/price_monitor",
        description="SQLAlchemy compatible database URL.",
    )
    redis_url: str = Field("redis://redis:6379/0", description="Redis connection URL used by Celery and caching.")

    telegram_bot_token: str = Field(
        "test-token",
        description="Telegram bot token issued by BotFather.",
    )
    telegram_admin_ids: List[int] = Field(default_factory=list, description="List of Telegram user IDs with admin rights.")

    msklad_username: Optional[str] = Field(None, description="Username for MoySklad API authentication.")
    msklad_password: Optional[str] = Field(None, description="Password for MoySklad API authentication.")
    msklad_token: Optional[str] = Field(None, description="API token for MoySklad if basic auth is not used.")
    msklad_account_url: AnyHttpUrl = Field(
        "https://online.moysklad.ru/api/remap/1.2",
        description="Base URL of the MoySklad API.",
    )

    default_price_types: List[str] = Field(
        default_factory=lambda: ["Цена продажи"],
        description="Default list of MoySklad price types to update if not specified explicitly.",
    )

    scheduler_timezone: str = Field("Europe/Moscow", description="Timezone used by the scheduler.")
    default_poll_interval_minutes: int = Field(
        15,
        description="Default poll interval for products in minutes when no specific value is configured.",
    )
    max_concurrent_requests: int = Field(4, description="Max concurrent Playwright/browser sessions per site.")
    http_timeout: int = Field(20, description="HTTP request timeout in seconds when scraping pages.")
    http_retries: int = Field(3, description="Number of retries for failed HTTP requests.")
    anti_bot_delay_seconds: int = Field(3, description="Delay between retries when anti-bot mechanisms are detected.")
    user_agent: str = Field(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        description="Default User-Agent header for scraping requests.",
    )
    notification_email: Optional[str] = Field(None, description="Optional e-mail for receiving price change alerts.")

    playwright_headless: bool = Field(True, description="Run Playwright browser in headless mode.")
    playwright_slow_mo: int = Field(0, description="Optional slow motion delay for Playwright actions.")

    structlog_json: bool = Field(False, description="Enable JSON output for structlog if true.")

    price_check_batch_size: int = Field(
        20,
        description="How many products are processed by the scheduler in a single batch.",
    )

    category_bulk_limit: int = Field(200, description="Max number of products to bulk bind from a single category.")

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator("default_price_types", mode="before")
    @classmethod
    def _parse_price_types(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @classmethod
    def _parse_env_var(cls, field_name: str, raw_value: str) -> object:
        """Custom environment variable parsing.

        Pydantic expects list-typed fields to be provided as JSON strings when
        loading from environment variables. Our deployment uses simple comma
        separated values (and sometimes leaves the value empty), which caused
        ``json.loads`` to fail before our validators could normalize the data.

        This hook converts the raw environment values into Python lists so that
        Pydantic can successfully coerce them into the desired field types.
        """

        if field_name == "default_price_types":
            if not raw_value.strip():
                return []
            return [item.strip() for item in raw_value.split(",") if item.strip()]

        if field_name == "telegram_admin_ids":
            if not raw_value.strip():
                return []
            return [int(item.strip()) for item in raw_value.split(",") if item.strip()]

        return raw_value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()  # type: ignore[arg-type]


settings = get_settings()

__all__ = ["Settings", "get_settings", "settings"]
