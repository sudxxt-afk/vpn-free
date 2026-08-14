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
    source_refresh_minutes: int = 20
    health_check_minutes: int = 3
    health_probe_batch_size: int = 100
    health_probe_concurrency: int = 3
    health_probe_timeout_seconds: float = 4.0
    health_probe_fresh_minutes: int = 45
    health_failure_grace_minutes: int = 15
    health_retry_seconds: int = 75
    health_min_pass_interval_seconds: int = 60
    health_probe_history_days: int = 14
    health_probe_speed_fresh_hours: int = 6
    health_probe_urls: str = "https://cp.cloudflare.com/generate_204,https://www.gstatic.com/generate_204,https://connectivitycheck.platform.hicloud.com/generate_204"
    health_probe_required_successes: int = 1
    health_probe_speed_url: str = "https://speed.cloudflare.com/__down?bytes=262144"
    health_probe_min_speed_kbps: float = 128.0
    health_scheduler_jitter_seconds: int = 60
    source_scheduler_jitter_seconds: int = 120
    xray_binary: str = "/usr/local/bin/xray"
    subscription_max_per_source: int = 12
    subscription_max_per_host: int = 1
    membership_check_hours: int = 12
    piarflow_api_key: str = ""
    piarflow_base_url: str = "https://piarflow.com/v1"
    piarflow_enabled: bool = False
    piarflow_webhook_secret: str = ""
    piarflow_stats_sync_hours: int = 6
    piarflow_stats_backfill_days: int = 90
    piarflow_stats_backfill_batch: int = 10
    api_rate_limit_per_minute: int = 120
    subscription_rate_limit_per_minute: int = 20
    login_rate_limit_per_15_minutes: int = 10
    alert_check_minutes: int = 15
    alert_cooldown_minutes: int = 60
    node_drop_alert_ratio: float = 0.5
    tls_alert_days: int = 14
    ton_donation_address: str = ""
    toncenter_base_url: str = "https://toncenter.com/api/v2"
    toncenter_api_key: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_telegram_ids.split(",") if value.strip().isdigit()}

    @property
    def probe_urls(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.health_probe_urls.split(",") if value.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
