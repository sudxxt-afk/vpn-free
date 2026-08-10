"""One-time PiarFlow onboarding, unsubscribe enforcement, and provider analytics."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (Device, PiarFlowAccessState, PiarFlowBotSnapshot, PiarFlowDailyStat, PiarFlowEvent,
                        TelegramUser)
from app.services.subscriptions import retire_user_devices

logger = logging.getLogger(__name__)

MAX_SPONSORS = 3
COMPLETED_STATUSES = {"subscribed", "not_counted"}
ALLOWED_STATES = {"completed", "deferred_no_inventory"}


@dataclass(frozen=True)
class Sponsor:
    link: str
    title: str = "Партнёрский канал"
    button_text: str = "Открыть задание"


@dataclass(frozen=True)
class PartnerDecision:
    allowed: bool
    status: str = "new"
    sponsors: tuple[Sponsor, ...] = ()
    sponsor_total: int = 0
    reason: str | None = None


class PiarFlowError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_links(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.startswith(("https://", "tg://"))]


def _sponsors_from_links(links: list[str]) -> tuple[Sponsor, ...]:
    return tuple(Sponsor(link=link) for link in links)


def _provider_sponsors(items: object) -> tuple[list[str], list[str]]:
    all_links: list[str] = []
    pending: list[str] = []
    if not isinstance(items, list):
        return all_links, pending
    for item in items:
        if not isinstance(item, dict):
            continue
        link = item.get("link")
        if not isinstance(link, str) or not link.startswith(("https://", "tg://")):
            continue
        all_links.append(link)
        if item.get("status") not in COMPLETED_STATUSES:
            pending.append(link)
    return all_links, pending


def _state(db: Session, user: TelegramUser) -> PiarFlowAccessState:
    state = db.get(PiarFlowAccessState, user.id)
    if state is None:
        state = PiarFlowAccessState(user_id=user.id)
        db.add(state)
        db.flush()
    return state


def _event(
    db: Session,
    event_type: str,
    user: TelegramUser | None,
    *,
    task_count: int = 0,
    revoked_devices: int = 0,
    details: dict | None = None,
) -> None:
    db.add(PiarFlowEvent(
        user_id=user.id if user else None,
        event_type=event_type,
        task_count=task_count,
        revoked_devices=revoked_devices,
        details_json=json.dumps(details or {}, ensure_ascii=False),
    ))


async def _request(method: str, path: str, *, payload: dict | None = None, params: dict | None = None) -> dict:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                f"{settings.piarflow_base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {settings.piarflow_api_key}"},
                json=payload,
                params=params,
            )
        if response.status_code == 404:
            raise PiarFlowError("Задания не найдены", 404)
        body = response.json()
        response.raise_for_status()
    except PiarFlowError:
        raise
    except httpx.HTTPStatusError as exc:
        raise PiarFlowError(str(exc), exc.response.status_code) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise PiarFlowError(str(exc)) from exc
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise PiarFlowError(str(body.get("message") if isinstance(body, dict) else "invalid response"))
    return body


def current_partner_access(db: Session, user: TelegramUser) -> PartnerDecision:
    settings = get_settings()
    if not settings.piarflow_enabled or not settings.piarflow_api_key:
        return PartnerDecision(True, status="disabled")
    state = db.get(PiarFlowAccessState, user.id)
    if state is None:
        return PartnerDecision(False, status="new", reason="Сначала запустите бота командой /start")
    pending = _json_links(state.pending_links_json)
    return PartnerDecision(
        state.status in ALLOWED_STATES,
        status=state.status,
        sponsors=_sponsors_from_links(pending),
        sponsor_total=state.task_count,
        reason=state.last_error,
    )


async def get_partner_access(db: Session, user: TelegramUser, target_devices: int = 1) -> PartnerDecision:
    """Advance onboarding only when /start or the explicit check action calls it."""
    del target_devices
    settings = get_settings()
    if not settings.piarflow_enabled or not settings.piarflow_api_key:
        return PartnerDecision(True, status="disabled")

    state = _state(db, user)
    if state.status == "completed":
        return PartnerDecision(True, status=state.status, sponsor_total=state.task_count)

    now = _now()
    if state.status == "pending":
        links = _json_links(state.links_json)
        state.last_checked_at = now
        _event(db, "check_attempt", user, task_count=state.task_count)
        try:
            body = await _request("POST", "/sponsors/check", payload={"user_id": user.telegram_id, "links": links})
            _all, pending = _provider_sponsors(body.get("sponsors"))
            state.pending_links_json = json.dumps(pending)
            state.last_error = None
            if pending:
                _event(db, "check_partial", user, task_count=len(pending))
                db.commit()
                return PartnerDecision(
                    False,
                    status="pending",
                    sponsors=_sponsors_from_links(pending),
                    sponsor_total=state.task_count,
                    reason="Подпишитесь на оставшиеся каналы и проверьте снова",
                )
            state.status = "completed"
            state.completed_at = now
            _event(db, "completed", user, task_count=state.task_count)
            db.commit()
            return PartnerDecision(True, status="completed", sponsor_total=state.task_count)
        except PiarFlowError as exc:
            state.last_error = "Проверка PiarFlow временно недоступна. Попробуйте позже."
            _event(db, "api_error", user, task_count=state.task_count, details={"status_code": exc.status_code})
            db.commit()
            return PartnerDecision(False, status="pending", sponsors=_sponsors_from_links(_json_links(state.pending_links_json)),
                                   sponsor_total=state.task_count, reason=state.last_error)

    previous_status = state.status
    state.prompted_at = state.prompted_at or now
    _event(db, "barrier_shown", user)
    try:
        body = await _request("POST", "/sponsors", payload={
            "user_id": user.telegram_id,
            "chat_id": user.telegram_id,
            "max_sponsors": MAX_SPONSORS,
        })
        links, pending = _provider_sponsors(body.get("sponsors"))
        if not links:
            raise PiarFlowError("Задания не найдены", 404)
        revoked = retire_user_devices(db, user.id, "sponsor_required") if previous_status == "deferred_no_inventory" else 0
        state.status = "pending"
        state.links_json = json.dumps(links)
        state.pending_links_json = json.dumps(pending or links)
        state.task_count = len(links)
        state.completed_at = None
        state.last_error = None
        _event(db, "tasks_issued", user, task_count=len(links), revoked_devices=revoked)
        db.commit()
        return PartnerDecision(False, status="pending", sponsors=_sponsors_from_links(pending or links),
                               sponsor_total=len(links), reason="Выполните задания PiarFlow")
    except PiarFlowError as exc:
        if exc.status_code == 404:
            state.status = "deferred_no_inventory"
            state.links_json = "[]"
            state.pending_links_json = "[]"
            state.task_count = 0
            state.last_error = None
            _event(db, "no_inventory", user)
            db.commit()
            return PartnerDecision(True, status=state.status, reason="Сейчас нет доступных заданий")
        state.last_error = "PiarFlow временно недоступен. Попробуйте позже."
        _event(db, "api_error", user, details={"status_code": exc.status_code})
        db.commit()
        return PartnerDecision(False, status=state.status, reason=state.last_error)


def handle_unsubscribe(db: Session, telegram_id: int, offer_link: str, bot_id: int | None = None) -> int:
    user = db.scalar(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    if user is None:
        _event(db, "unsubscribe_unknown", None, details={"telegram_id": telegram_id, "bot_id": bot_id})
        db.commit()
        return 0
    state = _state(db, user)
    if state.status == "unsubscribed" and db.scalar(select(Device.id).where(Device.user_id == user.id).limit(1)) is None:
        return 0
    revoked = retire_user_devices(db, user.id, "sponsor_unsubscribed")
    state.status = "unsubscribed"
    state.links_json = "[]"
    state.pending_links_json = "[]"
    state.task_count = 0
    state.completed_at = None
    state.last_error = None
    _event(db, "unsubscribed", user, revoked_devices=revoked, details={"offer_link": offer_link, "bot_id": bot_id})
    db.commit()
    return revoked


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def sync_piarflow_stats(db: Session) -> int:
    settings = get_settings()
    if not settings.piarflow_enabled or not settings.piarflow_api_key:
        return 0
    snapshot = db.get(PiarFlowBotSnapshot, 1)
    if snapshot is None:
        snapshot = PiarFlowBotSnapshot(id=1)
        db.add(snapshot)
        db.flush()
    today = _now().date()
    first_day = today - timedelta(days=max(1, settings.piarflow_stats_backfill_days) - 1)
    existing = set(db.scalars(select(PiarFlowDailyStat.date).where(PiarFlowDailyStat.date >= first_day)).all())
    missing = [first_day + timedelta(days=offset) for offset in range((today - first_day).days + 1)
               if first_day + timedelta(days=offset) not in existing]
    due = _aware(snapshot.last_synced_at) is None or _now() - _aware(snapshot.last_synced_at) >= timedelta(hours=max(1, settings.piarflow_stats_sync_hours))
    if not missing and not due:
        return 0
    try:
        profile = await _request("GET", "/traffic_bot")
        bot = profile.get("bot") or profile.get("data") or profile
        for field in ("bot_id", "chat_id", "username", "title", "topic", "is_active", "max_sponsors", "reset_time", "sold_subs", "not_counted"):
            if field in bot:
                setattr(snapshot, field, bot[field])
        snapshot.earned = float(bot.get("earned") or 0)
        dates = list(dict.fromkeys([today, today - timedelta(days=1), today - timedelta(days=2)] + missing[:max(1, settings.piarflow_stats_backfill_batch)]))
        synced = 0
        for stat_date in dates:
            if stat_date < first_day:
                continue
            body = await _request("GET", "/traffic_bot/stats", params={"date": str(stat_date)})
            values = body.get("stats") or {}
            row = db.get(PiarFlowDailyStat, stat_date)
            if row is None:
                row = PiarFlowDailyStat(date=stat_date)
                db.add(row)
            row.sold_subs = int(values.get("sold_subs") or 0)
            row.earned = float(values.get("earned") or 0)
            synced += 1
        snapshot.last_synced_at = _now()
        snapshot.last_error = None
        db.commit()
        return synced
    except PiarFlowError as exc:
        snapshot.last_error = str(exc)[:1000]
        db.commit()
        raise
