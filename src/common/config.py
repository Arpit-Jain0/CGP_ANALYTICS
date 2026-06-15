"""Application-wide settings, loaded once from environment variables."""

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="cpg_analytics", alias="POSTGRES_DB")
    postgres_user: str = Field(default="cpg", alias="POSTGRES_USER")
    postgres_password: str = Field(default="changeme", alias="POSTGRES_PASSWORD")

    # Allow an explicit override DSN (e.g. set by docker-compose)
    database_url_override: str | None = Field(default=None, alias="DATABASE_URL")

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── DeepSeek LLM ─────────────────────────────────────────────────────────
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    @property
    def llm_enabled(self) -> bool:
        """False when no API key is set; callers must use the deterministic fallback."""
        return bool(self.deepseek_api_key)

    # ── Application ───────────────────────────────────────────────────────────
    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Data paths ────────────────────────────────────────────────────────────
    data_input_historical: str = Field(
        default="data/input/historical", alias="DATA_INPUT_HISTORICAL"
    )
    data_input_incremental: str = Field(
        default="data/input/incremental", alias="DATA_INPUT_INCREMENTAL"
    )
    data_output_processed: str = Field(
        default="data/output/processed", alias="DATA_OUTPUT_PROCESSED"
    )
    data_output_quality_reports: str = Field(
        default="data/output/quality_reports", alias="DATA_OUTPUT_QUALITY_REPORTS"
    )
    data_output_archive: str = Field(
        default="data/output/archive", alias="DATA_OUTPUT_ARCHIVE"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()
