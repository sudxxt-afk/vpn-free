import asyncio
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import exists, func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import AdminUser, BroadcastCampaign, BroadcastDelivery, Device, TelegramUser

logger = logging.getLogger(__name__)
settings = get_settings()


def _recipient_query(segment: str):
    query = select(TelegramUser).where(TelegramUser.bot_blocked_at.is_(None))
    active_device = exists(select(Device.id).where(Device.user_id == TelegramUser.id, Device.is_revoked.is_(False)))
    if segment == "active":
        query = query.where(TelegramUser.is_blocked.is_(False))
    elif segment == "with_devices":
        query = query.where(active_device)
    elif segment == "without_devices":
        query = query.where(~active_device)
    return query


def _prepare_deliveries(campaign_id) -> None:
    with SessionLocal() as db:
        campaign = db.get(BroadcastCampaign, campaign_id)
        if not campaign:
            return
        existing = db.scalar(select(func.count()).select_from(BroadcastDelivery).where(BroadcastDelivery.campaign_id == campaign.id)) or 0
        if existing:
            return
        users = db.scalars(_recipient_query(campaign.segment)).all()
        for user in users:
            db.add(BroadcastDelivery(campaign_id=campaign.id, user_id=user.id))
        campaign.total_count = len(users)
        campaign.status = "processing"
        campaign.started_at = campaign.started_at or datetime.now(timezone.utc)
        db.commit()
        logger.info("Broadcast campaign=%s prepared recipients=%s", campaign.id, len(users))


async def _send_delivery(bot: Bot, campaign: BroadcastCampaign, chat_id: int) -> None:
    buttons = json.loads(campaign.buttons_json or "[]")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item["text"], url=item["url"])] for item in buttons
    ]) if buttons else None
    if campaign.photo_file_id:
        await bot.send_photo(chat_id, campaign.photo_file_id, caption=campaign.text_html or None, reply_markup=markup)
    else:
        await bot.send_message(chat_id, campaign.text_html, disable_web_page_preview=True, reply_markup=markup)


async def _process_campaign(campaign_id) -> None:
    _prepare_deliveries(campaign_id)
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        while True:
            with SessionLocal() as db:
                campaign = db.get(BroadcastCampaign, campaign_id)
                if not campaign or campaign.status in {"completed", "cancelled"}:
                    return
                if campaign.cancel_requested:
                    campaign.status = "cancelled"
                    campaign.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    break
                row = db.execute(
                    select(BroadcastDelivery, TelegramUser)
                    .join(TelegramUser, TelegramUser.id == BroadcastDelivery.user_id)
                    .where(BroadcastDelivery.campaign_id == campaign.id,
                           BroadcastDelivery.status == "pending",
                           BroadcastDelivery.attempts < 3)
                    .order_by(BroadcastDelivery.id)
                    .limit(1)
                ).first()
                if not row:
                    campaign.sent_count = db.scalar(select(func.count()).select_from(BroadcastDelivery).where(
                        BroadcastDelivery.campaign_id == campaign.id, BroadcastDelivery.status == "sent")) or 0
                    campaign.failed_count = db.scalar(select(func.count()).select_from(BroadcastDelivery).where(
                        BroadcastDelivery.campaign_id == campaign.id, BroadcastDelivery.status == "failed")) or 0
                    campaign.skipped_count = db.scalar(select(func.count()).select_from(BroadcastDelivery).where(
                        BroadcastDelivery.campaign_id == campaign.id, BroadcastDelivery.status == "skipped")) or 0
                    campaign.status = "completed"
                    campaign.finished_at = datetime.now(timezone.utc)
                    author = db.get(AdminUser, campaign.author_admin_id) if campaign.author_admin_id else None
                    summary = (campaign.sent_count, campaign.failed_count, campaign.skipped_count, campaign.total_count,
                               author.telegram_id if author else None)
                    db.commit()
                    if summary[4]:
                        try:
                            await bot.send_message(summary[4], f"✅ Рассылка завершена\nДоставлено: {summary[0]}\nОшибок: {summary[1]}\nПропущено: {summary[2]}\nВсего: {summary[3]}")
                        except Exception:
                            logger.exception("Unable to send broadcast completion notification")
                    return
                delivery, user = row
                delivery.attempts += 1
                try:
                    await _send_delivery(bot, campaign, user.telegram_id)
                except TelegramForbiddenError:
                    delivery.status = "skipped"
                    delivery.error = "bot_blocked"
                    user.bot_blocked_at = datetime.now(timezone.utc)
                    campaign.skipped_count += 1
                except TelegramRetryAfter as exc:
                    delivery.attempts -= 1
                    db.commit()
                    await asyncio.sleep(min(float(exc.retry_after) + 0.25, 60))
                    continue
                except Exception as exc:
                    delivery.error = str(exc)[:500]
                    if delivery.attempts >= 3:
                        delivery.status = "failed"
                        campaign.failed_count += 1
                    logger.warning("Broadcast delivery failed campaign=%s user=%s: %s", campaign.id, user.telegram_id, exc)
                else:
                    delivery.status = "sent"
                    delivery.sent_at = datetime.now(timezone.utc)
                    campaign.sent_count += 1
                db.commit()
            await asyncio.sleep(0.05)
    finally:
        await bot.session.close()


def process_broadcasts() -> None:
    if not settings.telegram_bot_token:
        return
    with SessionLocal() as db:
        campaign = db.scalars(select(BroadcastCampaign).where(BroadcastCampaign.status.in_(["queued", "processing"]))
                              .order_by(BroadcastCampaign.created_at).limit(1)).first()
        campaign_id = campaign.id if campaign else None
    if campaign_id:
        asyncio.run(_process_campaign(campaign_id))
