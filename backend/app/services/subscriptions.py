import base64
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, RetiredSubscription, SubscriptionCutover


BOT_DEEPLINK = "https://t.me/zazaaVPN_bot?start=reissue"
GLOBAL_REISSUE_MESSAGE = "Подписка отключена. Перевыпусти её в @zazaaVPN_bot"
SPONSOR_UNSUBSCRIBED_MESSAGE = "Ты отписался от спонсора. VPN отключён. Перейди в @zazaaVPN_bot и подпишись заново"


def retirement_message(reason: str) -> str:
    return SPONSOR_UNSUBSCRIBED_MESSAGE if reason in {"sponsor_unsubscribed", "sponsor_required"} else GLOBAL_REISSUE_MESSAGE


def happ_retirement_payload(reason: str) -> tuple[str, dict[str, str]]:
    message = retirement_message(reason)
    encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
    headers = {
        "Content-Disposition": "inline; filename=subscription.txt",
        "announce": f"base64:{encoded}",
        "support-url": BOT_DEEPLINK,
        "profile-title": "Zaza VPN - reissue required",
        "profile-update-interval": "1",
    }
    # An empty body removes cached proxy rows. HAPP treats metadata-only body
    # lines as proxy configs on some builds, so all notices stay in headers.
    return "", headers


def retire_user_devices(db: Session, user_id: UUID, reason: str, batch_id: UUID | None = None) -> int:
    devices = list(db.scalars(select(Device).where(Device.user_id == user_id)).all())
    for device in devices:
        db.add(RetiredSubscription(
            original_device_id=device.id,
            user_id=device.user_id,
            slot=device.slot,
            label=device.label,
            token_hash=device.token_hash,
            token_hint=device.token_hint,
            reason=reason,
            batch_id=batch_id,
            original_created_at=device.created_at,
            original_last_used_at=device.last_used_at,
        ))
        db.delete(device)
    db.flush()
    return len(devices)


def run_global_cutover(db: Session, cutover_key: str = "piarflow-onboarding-v1") -> SubscriptionCutover:
    existing = db.scalar(select(SubscriptionCutover).where(SubscriptionCutover.cutover_key == cutover_key))
    if existing:
        return existing
    cutover = SubscriptionCutover(cutover_key=cutover_key, reason="global_reissue")
    db.add(cutover)
    db.flush()
    user_ids = list(db.scalars(select(Device.user_id).distinct()).all())
    retired = sum(retire_user_devices(db, user_id, "global_reissue", cutover.id) for user_id in user_ids)
    cutover.retired_count = retired
    cutover.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cutover)
    return cutover


def rollback_global_cutover(db: Session, cutover_key: str = "piarflow-onboarding-v1") -> int:
    cutover = db.scalar(select(SubscriptionCutover).where(SubscriptionCutover.cutover_key == cutover_key))
    if not cutover:
        return 0
    retired = list(db.scalars(select(RetiredSubscription).where(RetiredSubscription.batch_id == cutover.id)).all())
    occupied = set(db.execute(select(Device.user_id, Device.slot)).all())
    active_hashes = set(db.scalars(select(Device.token_hash)).all())
    conflicts = [item for item in retired if (item.user_id, item.slot) in occupied or item.token_hash in active_hashes]
    if conflicts:
        raise RuntimeError("Нельзя откатить cutover: после него уже созданы конфликтующие устройства")
    for item in retired:
        db.add(Device(
            id=item.original_device_id,
            user_id=item.user_id,
            slot=item.slot,
            label=item.label,
            token_hash=item.token_hash,
            token_hint=item.token_hint,
            is_revoked=False,
            created_at=item.original_created_at,
            last_used_at=item.original_last_used_at,
        ))
        db.delete(item)
    db.delete(cutover)
    db.commit()
    return len(retired)
