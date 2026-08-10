"""PiarFlow sponsor tasks used to gate additional VPN device slots."""

from dataclasses import dataclass
import json
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PiarFlowTask, SponsorGate, TelegramUser

logger = logging.getLogger(__name__)

TIER_SPONSORS = {1: 4, 2: 3, 3: 3}
TIER_DEVICE_LIMITS = {1: 2, 2: 5, 3: 8}
COMPLETED_STATUSES = {"subscribed", "not_counted"}


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
    sponsor_total: int = 0
    reason: str | None = None


class PiarFlowError(RuntimeError):
    pass


def tier_for_devices(target_devices: int) -> int | None:
    for tier, limit in TIER_DEVICE_LIMITS.items():
        if target_devices <= limit:
            return tier
    return None


def tier_copy(tier: int) -> str:
    return {
        1: "4 партнёрских канала для первых двух устройств",
        2: "ещё 3 партнёрских канала для устройств 3-5",
        3: "ещё 3 партнёрских канала для устройств 6-8",
    }[tier]


def _links(task: PiarFlowTask | None) -> list[str]:
    if task is None:
        return []
    try:
        value = json.loads(task.links_json)
    except json.JSONDecodeError:
        return []
    return [item for item in value if isinstance(item, str) and item.startswith(("https://", "tg://"))] if isinstance(value, list) else []


def _sponsors(items: list[object]) -> tuple[Sponsor, ...]:
    sponsors: list[Sponsor] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") in COMPLETED_STATUSES:
            continue
        link = item.get("link")
        if isinstance(link, str) and link.startswith(("https://", "tg://")):
            sponsors.append(Sponsor(link=link, title="Партнёрский канал", button_text="Выполнить задание"))
    return tuple(sponsors)


async def _post(path: str, payload: dict) -> tuple[dict | None, int]:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{settings.piarflow_base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {settings.piarflow_api_key}"},
                json=payload,
            )
            body = response.json()
            if response.status_code == 404 and path == "/sponsors":
                return None, response.status_code
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise PiarFlowError(str(exc)) from exc
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise PiarFlowError(str(body.get("message") if isinstance(body, dict) else "invalid response"))
    return body, response.status_code


async def get_partner_access(db: Session, user: TelegramUser, target_devices: int) -> PartnerDecision:
    tier = tier_for_devices(target_devices)
    if tier is None:
        return PartnerDecision(False, reason="Доступно не более 8 устройств")

    gate = db.get(SponsorGate, user.id)
    if gate is None:
        gate = SponsorGate(user_id=user.id)
        db.add(gate)
        db.flush()
    if gate.completed_tier >= tier:
        return PartnerDecision(True, tier=tier)

    settings = get_settings()
    if not settings.piarflow_enabled or not settings.piarflow_api_key:
        logger.info("PiarFlow gate is disabled; allowing access without sponsor gate")
        return PartnerDecision(True, tier=tier)

    task = db.get(PiarFlowTask, user.id)
    links = _links(task) if task and task.tier == tier else []
    try:
        if links:
            body, _status = await _post("/sponsors/check", {"user_id": user.telegram_id, "links": links})
            checked = _sponsors(list((body or {}).get("sponsors") or []))
            if checked:
                return PartnerDecision(
                    False,
                    tier=tier,
                    sponsors=checked,
                    sponsor_total=len(links),
                    reason=tier_copy(tier),
                )
            gate.completed_tier = tier
            gate.pending_tier = None
            db.delete(task)
            db.commit()
            return PartnerDecision(True, tier=tier)

        body, status_code = await _post("/sponsors", {
            "user_id": user.telegram_id,
            "chat_id": user.telegram_id,
            "max_sponsors": TIER_SPONSORS[tier],
        })
        if body is None and status_code == 404:
            gate.completed_tier = tier
            gate.pending_tier = None
            db.commit()
            return PartnerDecision(True, tier=tier)
        sponsors = _sponsors(list((body or {}).get("sponsors") or []))
        if not sponsors:
            gate.completed_tier = tier
            gate.pending_tier = None
            db.commit()
            return PartnerDecision(True, tier=tier)
        if task is None:
            task = PiarFlowTask(user_id=user.id, tier=tier, links_json=json.dumps([item.link for item in sponsors]))
            db.add(task)
        else:
            task.tier = tier
            task.links_json = json.dumps([item.link for item in sponsors])
        gate.pending_tier = tier
        db.commit()
        return PartnerDecision(
            False,
            tier=tier,
            sponsors=sponsors,
            sponsor_total=len(sponsors),
            reason=tier_copy(tier),
        )
    except PiarFlowError as exc:
        logger.warning("PiarFlow request failed, denying access: %s", exc)
        return PartnerDecision(False, tier=tier, reason="Партнёрская проверка временно недоступна. Попробуйте позже.")
