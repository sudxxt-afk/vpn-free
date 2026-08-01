import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AnalyticsEvent, Donation, TelegramUser
from app.security import hash_token


STAR_PRESETS = {50, 100, 250, 500}
TON_PRESETS = (1, 2, 5, 10)


class DonationError(ValueError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_star_donation(db: Session, user: TelegramUser, amount: int) -> Donation:
    if not 1 <= amount <= 10000:
        raise DonationError("Сумма должна быть от 1 до 10 000 Stars")
    donation_id = uuid.uuid4()
    item = Donation(
        id=donation_id,
        user_id=user.id,
        method="stars",
        status="pending",
        amount_stars=amount,
        invoice_payload=f"zaza:stars:{donation_id.hex}",
    )
    db.add(item)
    db.add(AnalyticsEvent(event_type="donation_invoice", telegram_user_id=user.id))
    db.commit()
    db.refresh(item)
    return item


def validate_star_checkout(db: Session, user: TelegramUser, invoice_payload: str, currency: str, total_amount: int) -> Donation:
    item = db.scalar(select(Donation).where(Donation.invoice_payload == invoice_payload, Donation.user_id == user.id))
    if not item or item.method != "stars" or item.status != "pending":
        raise DonationError("Счёт не найден или уже обработан")
    if currency != "XTR" or item.amount_stars != total_amount:
        raise DonationError("Сумма или валюта счёта не совпадает")
    return item


def complete_star_donation(
    db: Session,
    user: TelegramUser,
    *,
    invoice_payload: str,
    currency: str,
    total_amount: int,
    telegram_payment_charge_id: str,
    provider_payment_charge_id: str | None,
) -> Donation:
    item = db.scalar(
        select(Donation).where(Donation.invoice_payload == invoice_payload, Donation.user_id == user.id).with_for_update()
    )
    if not item or item.method != "stars":
        raise DonationError("Счёт не найден")
    if item.status == "paid" and item.telegram_payment_charge_id == telegram_payment_charge_id:
        return item
    if item.status != "pending" or currency != "XTR" or item.amount_stars != total_amount:
        raise DonationError("Платёж не соответствует ожидающему счёту")
    duplicate = db.scalar(select(Donation).where(Donation.telegram_payment_charge_id == telegram_payment_charge_id))
    if duplicate and duplicate.id != item.id:
        raise DonationError("Этот платёж уже обработан")
    item.status = "paid"
    item.telegram_payment_charge_id = telegram_payment_charge_id
    item.provider_payment_charge_id = provider_payment_charge_id or None
    item.paid_at = datetime.now(timezone.utc)
    db.add(AnalyticsEvent(event_type="donation_paid", telegram_user_id=user.id))
    db.commit()
    db.refresh(item)
    return item


def create_ton_session(db: Session, user: TelegramUser) -> tuple[Donation, str]:
    settings = get_settings()
    if not settings.ton_donation_address.strip():
        raise DonationError("TON-донаты пока не настроены")
    token = secrets.token_urlsafe(32)
    item = Donation(
        id=uuid.uuid4(),
        user_id=user.id,
        method="ton",
        status="pending",
        public_token_hash=hash_token(token),
    )
    db.add(item)
    db.add(AnalyticsEvent(event_type="donation_ton_open", telegram_user_id=user.id))
    db.commit()
    db.refresh(item)
    return item, token


def donation_by_public_token(db: Session, token: str) -> Donation:
    item = db.scalar(select(Donation).where(Donation.public_token_hash == hash_token(token), Donation.method == "ton"))
    if not item:
        raise DonationError("Сессия доната не найдена")
    if item.status == "pending" and item.created_at and _aware(item.created_at) < datetime.now(timezone.utc) - timedelta(hours=48):
        item.status = "expired"
        db.commit()
    return item


def prepare_ton_donation(db: Session, item: Donation, amount: Decimal) -> Donation:
    amount = amount.quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    if amount < Decimal("0.1") or amount > Decimal("100"):
        raise DonationError("Сумма должна быть от 0.1 до 100 TON")
    amount_nano = int(amount * Decimal(1_000_000_000))
    if item.status != "pending":
        raise DonationError("Сессия уже завершена")
    if item.reference:
        if item.amount_nano != amount_nano:
            raise DonationError("Сумма уже зафиксирована; откройте новую сессию")
        return item
    item.amount_nano = amount_nano
    item.reference = f"zaza-{item.id.hex[:24]}"
    db.add(AnalyticsEvent(event_type="donation_ton_prepared", telegram_user_id=item.user_id))
    db.commit()
    db.refresh(item)
    return item


def public_ton_payload(item: Donation) -> dict:
    settings = get_settings()
    return {
        "status": item.status,
        "enabled": bool(settings.ton_donation_address.strip()),
        "recipient": settings.ton_donation_address.strip() if settings.ton_donation_address.strip() else None,
        "amount_nano": item.amount_nano,
        "amount_ton": round((item.amount_nano or 0) / 1_000_000_000, 3) if item.amount_nano else None,
        "reference": item.reference,
        "tx_hash": item.tx_hash,
        "presets": list(TON_PRESETS),
    }


def match_ton_transaction(item: Donation, transactions: list[dict]) -> dict | None:
    if not item.reference or not item.amount_nano:
        return None
    created_after = int(_aware(item.created_at).timestamp()) - 300
    for transaction in transactions:
        incoming = transaction.get("in_msg") or {}
        transaction_id = transaction.get("transaction_id") or {}
        try:
            value = int(incoming.get("value") or 0)
            timestamp = int(transaction.get("utime") or 0)
        except (TypeError, ValueError):
            continue
        tx_hash = str(transaction_id.get("hash") or incoming.get("hash") or "")
        if timestamp < created_after or value < item.amount_nano or incoming.get("message") != item.reference or not tx_hash:
            continue
        return {"tx_hash": tx_hash, "sender": str(incoming.get("source") or ""), "value": value}
    return None


async def _recent_transactions(oldest: datetime) -> list[dict]:
    settings = get_settings()
    if not settings.ton_donation_address.strip():
        return []
    headers = {"X-API-Key": settings.toncenter_api_key} if settings.toncenter_api_key else {}
    params: dict[str, str | int] = {"address": settings.ton_donation_address.strip(), "limit": 100}
    collected: list[dict] = []
    async with httpx.AsyncClient(base_url=settings.toncenter_base_url.rstrip("/"), timeout=15) as client:
        for _ in range(5):
            response = await client.get("/getTransactions", params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                raise httpx.HTTPError(str(body.get("error") or "TON Center rejected request"))
            page = body.get("result") or []
            if not page:
                break
            collected.extend(page)
            last = page[-1]
            if int(last.get("utime") or 0) < int(oldest.timestamp()) - 300:
                break
            transaction_id = last.get("transaction_id") or {}
            if not transaction_id.get("lt") or not transaction_id.get("hash"):
                break
            params["lt"] = str(transaction_id["lt"])
            params["hash"] = str(transaction_id["hash"])
    return collected


async def verify_pending_ton_donations(db: Session) -> list[tuple[int, float]]:
    pending = db.scalars(
        select(Donation).where(
            Donation.method == "ton",
            Donation.status == "pending",
            Donation.reference.is_not(None),
            Donation.created_at >= datetime.now(timezone.utc) - timedelta(hours=48),
        ).order_by(Donation.created_at)
    ).all()
    if not pending:
        return []
    transactions = await _recent_transactions(_aware(pending[0].created_at))
    settled: list[tuple[int, float]] = []
    for item in pending:
        match = match_ton_transaction(item, transactions)
        if not match:
            continue
        duplicate = db.scalar(select(Donation).where(Donation.tx_hash == match["tx_hash"], Donation.id != item.id))
        if duplicate:
            continue
        item.status = "paid"
        item.tx_hash = match["tx_hash"]
        item.sender_address = match["sender"] or None
        item.amount_nano = match["value"]
        item.paid_at = datetime.now(timezone.utc)
        db.add(AnalyticsEvent(event_type="donation_paid", telegram_user_id=item.user_id))
        user = db.get(TelegramUser, item.user_id)
        if user:
            settled.append((user.telegram_id, round(match["value"] / 1_000_000_000, 3)))
    db.commit()
    return settled


def donation_summary(db: Session, user: TelegramUser) -> dict:
    paid = db.scalars(select(Donation).where(Donation.user_id == user.id, Donation.status == "paid")).all()
    return {
        "donations": len(paid),
        "stars": sum(item.amount_stars or 0 for item in paid),
        "ton": round(sum(item.amount_nano or 0 for item in paid) / 1_000_000_000, 3),
        "ton_enabled": bool(get_settings().ton_donation_address.strip()),
    }


def donation_analytics(db: Session, since: datetime) -> dict:
    paid = db.scalars(select(Donation).where(Donation.status == "paid", Donation.paid_at >= since)).all()
    return {
        "donation_supporters": len({item.user_id for item in paid}),
        "donation_stars_count": sum(1 for item in paid if item.method == "stars"),
        "donation_stars_total": sum(item.amount_stars or 0 for item in paid if item.method == "stars"),
        "donation_ton_count": sum(1 for item in paid if item.method == "ton"),
        "donation_ton_total": round(sum(item.amount_nano or 0 for item in paid if item.method == "ton") / 1_000_000_000, 3),
    }
