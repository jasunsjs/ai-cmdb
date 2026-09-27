"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (OpenRouter, OpenAI-compatible API)
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "anthropic/claude-haiku-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Storage
    database_url: str = "sqlite:///data/cmdb.db"

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"


@lru_cache
def get_settings() -> Settings:
    return Settings()
