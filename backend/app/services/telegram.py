from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RequiredChannel, TelegramUser


async def validate_bot_admin(chat_id: int) -> bool:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=10) as client:
        me = (await client.get(f"{base}/getMe")).json()
        if not me.get("ok"):
            return False
        result = (await client.get(f"{base}/getChatMember", params={"chat_id": chat_id, "user_id": me["result"]["id"]})).json()
    return bool(result.get("ok") and result["result"].get("status") in {"administrator", "creator"})


async def has_required_memberships(db: Session, user: TelegramUser) -> bool:
    channels = db.scalars(select(RequiredChannel).where(RequiredChannel.is_active.is_(True))).all()
    if not channels:
        user.last_membership_check = datetime.now(timezone.utc)
        db.commit()
        return True
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=10) as client:
        for channel in channels:
            response = await client.get(f"{base}/getChatMember", params={"chat_id": channel.chat_id, "user_id": user.telegram_id})
            body = response.json()
            if not body.get("ok") or body["result"].get("status") in {"left", "kicked"}:
                return False
    user.last_membership_check = datetime.now(timezone.utc)
    db.commit()
    return True

