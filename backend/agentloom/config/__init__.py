"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values come from environment variables (prefixed AGENTLOOM_ when defined),
    or the untyped keys below when they match common provider conventions.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    environment: str = Field(default="dev", description="dev | test | prod")
    log_level: str = Field(default="INFO")

    # --- Database / cache ---
    database_url: str = Field(
        default="postgresql+asyncpg://agentloom:agentloom@localhost:5432/agentloom"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # --- Paths ---
    data_dir: str = Field(default="./data")
    workspace_root: str = Field(default="./data/workspaces")

    # --- Provider keys (read directly from environment for convenience) ---
    volcengine_api_key: str | None = Field(default=None, alias="VOLCENGINE_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")

    # --- Server ---
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance."""
    return Settings()
