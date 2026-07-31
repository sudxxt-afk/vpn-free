from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./vpn.db"
    redis_url: str = "redis://localhost:6379/0"
    app_secret: str = "development-secret-change-me"
    app_encryption_key: str | None = None
    initial_admin_login: str = "admin"
    initial_admin_password: str = "change-me-now"
    telegram_bot_token: str = ""
    admin_telegram_ids: str = ""
    internal_api_key: str = "development-internal-key"
    public_base_url: str = "http://localhost:8000"
    web_app_base_url: str = "http://localhost:5173"
    backend_internal_url: str = "http://api:8000"
    frontend_origin: str = "http://localhost:5173"
    source_refresh_minutes: int = 40
    health_check_minutes: int = 10
    membership_check_hours: int = 12

    @property
    def admin_ids(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_telegram_ids.split(",") if value.strip().isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
