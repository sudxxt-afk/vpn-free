"""Persistent one-time Subgram onboarding with webhook and polling revocation."""

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SubgramAccessState, SubgramSponsorState, TelegramUser
from app.services.subgram import AccessDecision, get_subgram_access, get_subgram_subscriptions


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def assigned_ads(state: SubgramAccessState) -> tuple[int, ...]:
    try:
        values = json.loads(state.assigned_ads_json)
    except (TypeError, ValueError):
        return ()
    if not isinstance(values, list):
        return ()
    result: list[int] = []
    for value in values:
        try:
            ads_id = int(value)
        except (TypeError, ValueError):
            continue
        if ads_id > 0 and ads_id not in result:
            result.append(ads_id)
    return tuple(result)


def has_sponsor_block(db: Session, user: TelegramUser) -> bool:
    state = db.get(SubgramAccessState, user.id)
    if state and state.blocked_at is not None:
        return True
    return (db.scalar(select(func.count()).select_from(SubgramSponsorState).where(
        SubgramSponsorState.telegram_id == user.telegram_id,
        SubgramSponsorState.status == "unsubscribed",
    )) or 0) > 0


def has_cached_access(db: Session, user: TelegramUser) -> bool:
    state = db.get(SubgramAccessState, user.id)
    return bool(state and state.verified_at is not None and not has_sponsor_block(db, user))


def _remember_decision(db: Session, user: TelegramUser, decision: AccessDecision) -> None:
    state = db.get(SubgramAccessState, user.id)
    if state is None and (decision.ads_ids or decision.status == "ok"):
        state = SubgramAccessState(user_id=user.id)
        db.add(state)
    if state is None:
        return
    existing = assigned_ads(state)
    if decision.ads_ids:
        state.assigned_ads_json = json.dumps(list(dict.fromkeys((*existing, *decision.ads_ids))))
    if decision.status == "ok":
        now = _utcnow()
        state.verified_at = state.verified_at or now
        state.blocked_at = None
        state.last_checked_at = now
        db.execute(update(SubgramSponsorState).where(
            SubgramSponsorState.telegram_id == user.telegram_id,
            SubgramSponsorState.status == "unsubscribed",
        ).values(status="verified"))


async def resolve_subgram_access(db: Session, user: TelegramUser) -> AccessDecision:
    """Reuse a completed onboarding unless a webhook or recheck revoked it."""
    if has_cached_access(db, user):
        return AccessDecision(True, "cached", reason="Подписка на спонсоров уже подтверждена")
    decision = await get_subgram_access(user.telegram_id, username=user.username)
    _remember_decision(db, user, decision)
    db.commit()
    return decision


def sync_access_blocks_from_webhooks(db: Session, telegram_ids: set[int]) -> None:
    """Reflect latest webhook states in the one-time access record."""
    now = _utcnow()
    for user in db.scalars(select(TelegramUser).where(TelegramUser.telegram_id.in_(telegram_ids))).all():
        state = db.get(SubgramAccessState, user.id)
        if state is None or state.verified_at is None:
            continue
        blocked = (db.scalar(select(func.count()).select_from(SubgramSponsorState).where(
            SubgramSponsorState.telegram_id == user.telegram_id,
            SubgramSponsorState.status == "unsubscribed",
        )) or 0) > 0
        state.blocked_at = now if blocked else None


async def recheck_due_access_states(db: Session, limit: int = 100) -> tuple[int, int]:
    """Recheck assigned sponsors without requesting or rotating sponsor tasks."""
    settings = get_settings()
    due_before = _utcnow() - timedelta(hours=max(1, settings.subgram_recheck_hours))
    rows = db.execute(
        select(SubgramAccessState, TelegramUser)
        .join(TelegramUser, TelegramUser.id == SubgramAccessState.user_id)
        .where(
            SubgramAccessState.verified_at.is_not(None),
            (SubgramAccessState.last_checked_at.is_(None) | (SubgramAccessState.last_checked_at < due_before)),
            TelegramUser.is_blocked.is_(False),
        )
        .order_by(SubgramAccessState.last_checked_at.asc().nullsfirst())
        .limit(max(1, limit))
    ).all()
    checked = blocked = 0
    for state, user in rows:
        ads_ids = assigned_ads(state)
        if not ads_ids:
            state.last_checked_at = _utcnow()
            continue
        review = await get_subgram_subscriptions(user.telegram_id, ads_ids)
        # Webhooks remain the primary revocation channel, so a failed check is
        # not retried hourly: Subgram answers 404 permanently for task pairs it
        # no longer knows.
        state.last_checked_at = _utcnow()
        if not review.available:
            continue
        statuses = dict(review.statuses)
        checked += 1
        if any(status == "unsubscribed" for status in statuses.values()):
            state.blocked_at = _utcnow()
            blocked += 1
        elif all(ads_id in statuses and statuses[ads_id] in {"subscribed", "subpin", "notgetted"} for ads_id in ads_ids):
            state.blocked_at = None
        for ads_id, status in statuses.items():
            db.execute(update(SubgramSponsorState).where(
                SubgramSponsorState.telegram_id == user.telegram_id,
                SubgramSponsorState.resource_key == f"ads:{ads_id}",
            ).values(status=status))
    db.commit()
    return checked, blocked
