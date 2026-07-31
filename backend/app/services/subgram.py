"""SubGram publisher integration used for device-access sponsorship gates."""

from dataclasses import dataclass
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PartnerGate, TelegramUser

logger = logging.getLogger(__name__)

TIER_SPONSORS = {1: 4, 2: 3, 3: 3}
TIER_DEVICE_LIMITS = {1: 2, 2: 5, 3: 8}


@dataclass(frozen=True)
class Sponsor:
    link: str
    title: str
    button_text: str


@dataclass(frozen=True)
class PartnerDecision:
    allowed: bool
    tier: int | None = None
    sponsors: tuple[Sponsor, ...] = ()
    reason: str | None = None


def tier_for_devices(target_devices: int) -> int | None:
    for tier, limit in TIER_DEVICE_LIMITS.items():
        if target_devices <= limit:
            return tier
    return None


def tier_copy(tier: int) -> str:
    return {
        1: "4 партнёрских канала для первых двух устройств",
        2: "ещё 3 партнёрских канала для устройств 3–5",
        3: "ещё 3 партнёрских канала для устройств 6–8",
    }[tier]


async def get_partner_access(db: Session, user: TelegramUser, target_devices: int) -> PartnerDecision:
    """Check or issue precisely one persistent SubGram sponsorship task per tier."""
    tier = tier_for_devices(target_devices)
    if tier is None:
        return PartnerDecision(False, reason="Доступно не более 8 устройств")

    gate = db.get(PartnerGate, user.id)
    if gate is None:
        gate = PartnerGate(user_id=user.id)
        db.add(gate)
        db.flush()
    if gate.completed_tier >= tier:
        return PartnerDecision(True, tier=tier)

    settings = get_settings()
    if not settings.subgram_api_key:
        logger.warning("SubGram is not configured; allowing access without partner gate")
        return PartnerDecision(True, tier=tier)

    action = "subscribe" if gate.pending_tier == tier else "newtask"
    payload = {"user_id": user.telegram_id, "chat_id": user.telegram_id, "action": action,
               "max_sponsors": TIER_SPONSORS[tier], "get_links": 1}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{settings.subgram_base_url.rstrip('/')}/get-sponsors",
                                         headers={"Auth": settings.subgram_api_key}, json=payload)
            body = response.json()
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SubGram request failed, allowing access: %s", exc)
        return PartnerDecision(True, tier=tier)

    response_status = str(body.get("status", "error"))
    if response_status == "ok":
        gate.completed_tier = tier
        gate.pending_tier = None
        db.commit()
        return PartnerDecision(True, tier=tier)
    if response_status != "warning":
        logger.warning("SubGram returned %s, allowing access: %s", response_status, body)
        return PartnerDecision(True, tier=tier)

    sponsors: list[Sponsor] = []
    for item in body.get("additional", {}).get("sponsors", []):
        if item.get("status") != "unsubscribed" or not item.get("available_now", True):
            continue
        link = item.get("link")
        if not isinstance(link, str) or not link.startswith(("https://", "tg://")):
            continue
        sponsors.append(Sponsor(link=link, title=str(item.get("resource_name") or "Партнёрский канал"),
                                button_text=str(item.get("button_text") or "Подписаться")))
    gate.pending_tier = tier
    db.commit()
    return PartnerDecision(False, tier=tier, sponsors=tuple(sponsors), reason=tier_copy(tier))
