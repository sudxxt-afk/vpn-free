"""Authenticated, idempotent handling of Subgram subscription events."""

from dataclasses import dataclass
from hashlib import sha256
import hmac

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SubgramSponsorState, SubgramWebhookEvent, TelegramUser
from app.schemas import SubgramWebhookEventPayload


@dataclass(frozen=True)
class WebhookResult:
    received: int
    processed: int
    duplicates: int
    stale: int


def expected_bot_id() -> int | None:
    token_prefix = get_settings().telegram_bot_token.partition(":")[0]
    return int(token_prefix) if token_prefix.isdigit() else None


def webhook_key_is_valid(api_key: str | None) -> bool:
    expected = get_settings().subgram_api_key
    return bool(expected and api_key and hmac.compare_digest(api_key, expected))


def _resource_key(item: SubgramWebhookEventPayload) -> tuple[str, str]:
    link_hash = sha256(item.link.encode("utf-8")).hexdigest()
    return (f"ads:{item.ads_id}" if item.ads_id is not None else f"link:{link_hash}", link_hash)


def process_webhooks(db: Session, items: list[SubgramWebhookEventPayload]) -> WebhookResult:
    ordered = sorted(items, key=lambda item: item.webhook_id)
    incoming_ids = {item.webhook_id for item in ordered}
    existing_ids = set(db.scalars(
        select(SubgramWebhookEvent.webhook_id).where(SubgramWebhookEvent.webhook_id.in_(incoming_ids))
    ).all())
    seen = set(existing_ids)
    users = {
        user.telegram_id: user
        for user in db.scalars(select(TelegramUser).where(
            TelegramUser.telegram_id.in_({item.user_id for item in ordered})
        )).all()
    }
    processed = duplicates = stale = 0
    for item in ordered:
        if item.webhook_id in seen:
            duplicates += 1
            continue
        seen.add(item.webhook_id)
        resource_key, link_hash = _resource_key(item)
        user = users.get(item.user_id)
        db.add(SubgramWebhookEvent(
            webhook_id=item.webhook_id,
            telegram_user_id=user.id if user else None,
            telegram_id=item.user_id,
            bot_id=item.bot_id,
            ads_id=item.ads_id,
            resource_key=resource_key,
            link_hash=link_hash,
            status=item.status,
            subscribe_date=item.subscribe_date,
        ))
        state = db.scalar(select(SubgramSponsorState).where(
            SubgramSponsorState.telegram_id == item.user_id,
            SubgramSponsorState.resource_key == resource_key,
        ))
        if state and state.latest_webhook_id >= item.webhook_id:
            stale += 1
        elif state:
            state.status = item.status
            state.latest_webhook_id = item.webhook_id
        else:
            db.add(SubgramSponsorState(
                telegram_id=item.user_id,
                resource_key=resource_key,
                status=item.status,
                latest_webhook_id=item.webhook_id,
            ))
        processed += 1
    return WebhookResult(len(items), processed, duplicates, stale)


def has_webhook_block(db: Session, telegram_id: int) -> bool:
    return db.scalar(select(func.count()).select_from(SubgramSponsorState).where(
        SubgramSponsorState.telegram_id == telegram_id,
        SubgramSponsorState.status == "unsubscribed",
    )) > 0


def clear_webhook_blocks_after_live_check(db: Session, telegram_id: int) -> int:
    result = db.execute(update(SubgramSponsorState).where(
        SubgramSponsorState.telegram_id == telegram_id,
        SubgramSponsorState.status == "unsubscribed",
    ).values(status="verified"))
    return result.rowcount or 0
