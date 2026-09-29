"""Application settings, loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (OpenRouter, OpenAI-compatible API)
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "anthropic/claude-haiku-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = 60.0
    # Also send the JSON schema as `response_format` (replies are validated either way).
    llm_structured_outputs: bool = True

    # Storage
    database_url: str = "sqlite:///data/cmdb.db"

    # Ingestion
    max_upload_bytes: int = 10 * 1024 * 1024
    # LLM entity matches at or above this confidence are merged automatically. 0.8 is where
    # the judge's calibration scale says "very likely the same". Set above 1.0 to never merge.
    entity_match_auto_merge_threshold: float = 0.8

    # Natural-language queries
    ask_max_rows: int = 200
    ask_timeout_seconds: float = 5.0

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        # `OPENROUTER_API_KEY=` (as copied from .env.example) means "not configured".
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
