from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level configuration, read from the environment (and `.env` when present).

    Anything an admin should be able to change at runtime lives in `runtime_settings` instead.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:////data/app.db"
    secret_key: str = "change-me"
    admin_username: str | None = None
    admin_password: str | None = None
    session_cookie_secure: bool = False

    claude_bin: str = "claude"
    # Persistent Claude Code state: `config/` (CLAUDE_CONFIG_DIR, holds session transcripts) and
    # `sessions/` (the fixed working directory resumable sessions run in).
    claude_data_dir: str = "/data/claude"
    claude_code_oauth_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
