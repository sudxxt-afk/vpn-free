import base64
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Device, RetiredSubscription, SubscriptionCutover, SubscriptionRestoration


BOT_DEEPLINK = "https://t.me/zazaaVPN_bot?start=reissue"
GLOBAL_REISSUE_MESSAGE = "Подписка отключена. Перевыпусти её в @zazaaVPN_bot"
SPONSOR_UNSUBSCRIBED_MESSAGE = "Ты отписался от спонсора. VPN отключён. Перейди в @zazaaVPN_bot и подпишись заново"
SPONSOR_ACCESS_MESSAGE = "Подпишись на каналы партнёров в @zazaaVPN_bot и обнови подписку"
RESTORE_CAMPAIGN_KEY = "sponsor-onboarding-v1"


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


def happ_sponsor_gate_payload() -> tuple[str, dict[str, str]]:
    """Temporarily clear cached nodes while Subgram reports missing subscriptions."""
    encoded = base64.b64encode(SPONSOR_ACCESS_MESSAGE.encode("utf-8")).decode("ascii")
    return "", {
        "Content-Disposition": "inline; filename=subscription.txt",
        "announce": f"base64:{encoded}",
        "support-url": "https://t.me/zazaaVPN_bot?start=sponsors",
        "profile-title": "Zaza VPN - subscribe to sponsors",
        "profile-update-interval": "1",
    }


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


def retire_device(db: Session, device: Device, reason: str) -> None:
    archive_device_token(db, device, reason)
    db.delete(device)
    db.flush()


def archive_device_token(db: Session, device: Device, reason: str) -> None:
    db.add(RetiredSubscription(
        original_device_id=device.id,
        user_id=device.user_id,
        slot=device.slot,
        label=device.label,
        token_hash=device.token_hash,
        token_hint=device.token_hint,
        reason=reason,
        original_created_at=device.created_at,
        original_last_used_at=device.last_used_at,
    ))
    db.flush()


def restoration_candidate(db: Session, user_id: UUID) -> RetiredSubscription | None:
    return db.scalar(select(RetiredSubscription).where(
        RetiredSubscription.user_id == user_id,
        RetiredSubscription.reason == "global_reissue",
    ).order_by(RetiredSubscription.original_last_used_at.desc().nullslast(), RetiredSubscription.retired_at.desc()))


def subscription_can_be_restored(db: Session, user_id: UUID, active_devices: int | None = None) -> bool:
    if active_devices is None:
        active_devices = db.scalar(select(func.count()).select_from(Device).where(
            Device.user_id == user_id, Device.is_revoked.is_(False)
        )) or 0
    if active_devices:
        return False
    restored = db.scalar(select(SubscriptionRestoration.id).where(
        SubscriptionRestoration.user_id == user_id,
        SubscriptionRestoration.campaign_key == RESTORE_CAMPAIGN_KEY,
    ))
    return restored is None and restoration_candidate(db, user_id) is not None


def record_subscription_restoration(db: Session, user_id: UUID, device_id: UUID) -> SubscriptionRestoration:
    item = SubscriptionRestoration(
        user_id=user_id,
        device_id=device_id,
        campaign_key=RESTORE_CAMPAIGN_KEY,
    )
    db.add(item)
    db.flush()
    return item


def reconcile_existing_restorations(db: Session) -> int:
    """Mark users who already created a device after the historical cutover."""
    eligible = set(db.scalars(select(RetiredSubscription.user_id).where(
        RetiredSubscription.reason == "global_reissue"
    )).all())
    active: dict[UUID, UUID] = {}
    if eligible:
        for device in db.scalars(select(Device).where(
            Device.is_revoked.is_(False), Device.user_id.in_(eligible)
        ).order_by(Device.created_at)).all():
            active.setdefault(device.user_id, device.id)
    existing = set(db.scalars(select(SubscriptionRestoration.user_id).where(
        SubscriptionRestoration.campaign_key == RESTORE_CAMPAIGN_KEY,
        SubscriptionRestoration.user_id.in_(active),
    )).all()) if active else set()
    for user_id, device_id in active.items():
        if user_id not in existing:
            record_subscription_restoration(db, user_id, device_id)
    if active.keys() - existing:
        db.commit()
    return len(active.keys() - existing)


def run_global_cutover(db: Session, cutover_key: str = "sponsor-onboarding-v1") -> SubscriptionCutover:
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


def rollback_global_cutover(db: Session, cutover_key: str = "sponsor-onboarding-v1") -> int:
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
