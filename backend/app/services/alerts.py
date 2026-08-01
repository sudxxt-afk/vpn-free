import logging

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import AdminUser, Role

logger = logging.getLogger(__name__)


async def should_alert(key: str) -> bool:
    """Avoid repeating the same operational alert every scheduler interval."""
    settings = get_settings()
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        try:
            return bool(await client.set(f"alert:{key}", "1", ex=settings.alert_cooldown_minutes * 60, nx=True))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning("Alert throttle unavailable; sending alert: %s", exc)
        return True


async def notify_admins(message: str) -> None:
    """Best-effort Telegram alert; alerts must never stop a scheduled job."""
    settings = get_settings()
    try:
        with SessionLocal() as db:
            database_ids = set(db.scalars(select(AdminUser.telegram_id).where(
                AdminUser.is_active.is_(True), AdminUser.support_enabled.is_(True),
                AdminUser.telegram_id.is_not(None), AdminUser.role.in_([Role.OWNER, Role.ADMIN]))).all())
    except Exception as exc:
        logger.warning("Unable to load alert recipients from database: %s", exc)
        database_ids = set()
    chat_ids = database_ids or settings.admin_ids
    if not settings.telegram_bot_token or not chat_ids:
        return
    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for chat_id in chat_ids:
                await client.post(endpoint, json={"chat_id": chat_id, "text": f"⚠️ Zaza VPN alert\n{message}"})
    except httpx.HTTPError as exc:
        logger.warning("Unable to deliver Telegram alert: %s", exc)
