"""Live Subgram sponsor access checks without webhooks or local access state."""

from dataclasses import dataclass
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sponsor:
    link: str
    title: str = "Канал партнёра"
    button_text: str = "Подписаться"
    resource_id: str | None = None
    resource_type: str | None = None


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    status: str
    sponsors: tuple[Sponsor, ...] = ()
    sponsor_total: int = 0
    reason: str | None = None


def _safe_text(value: object, fallback: str, limit: int = 80) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] or fallback


def _sponsors(body: dict) -> tuple[Sponsor, ...]:
    additional = body.get("additional")
    items = additional.get("sponsors") if isinstance(additional, dict) else None
    result: list[Sponsor] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("available_now") is not True or item.get("status") == "subscribed":
                continue
            link = item.get("link")
            if not isinstance(link, str) or not link.startswith(("https://", "tg://")):
                continue
            result.append(Sponsor(
                link=link,
                title=_safe_text(item.get("resource_name"), "Канал партнёра"),
                button_text=_safe_text(item.get("button_text"), "Подписаться", 32),
                resource_id=str(item["resource_id"]) if item.get("resource_id") is not None else None,
                resource_type=str(item["type"]) if item.get("type") is not None else None,
            ))
    if result:
        return tuple(result)

    links = body.get("links")
    if isinstance(links, list):
        return tuple(
            Sponsor(link=link)
            for link in links
            if isinstance(link, str) and link.startswith(("https://", "tg://"))
        )
    return ()


async def _request(payload: dict) -> tuple[int, dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
        response = await client.post(
            f"{settings.subgram_base_url.rstrip('/')}/get-sponsors",
            headers={"Auth": settings.subgram_api_key, "Content-Type": "application/json"},
            json=payload,
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


async def get_subgram_access(
    telegram_id: int,
    *,
    chat_id: int | None = None,
    username: str | None = None,
) -> AccessDecision:
    """Return a live access decision following Subgram's documented fail-open policy."""
    settings = get_settings()
    if not settings.subgram_api_key:
        return AccessDecision(True, "disabled")

    payload: dict[str, object] = {
        "user_id": telegram_id,
        "chat_id": chat_id or telegram_id,
        "action": "subscribe",
        "max_sponsors": max(1, min(settings.subgram_max_sponsors, 10)),
        "get_links": 1,
    }
    if username:
        payload["username"] = username.lstrip("@")[:32]

    try:
        status_code, body = await _request(payload)
    except httpx.HTTPError as exc:
        logger.warning("Subgram request failed; allowing access: %s", exc.__class__.__name__)
        return AccessDecision(True, "error", reason="Subgram временно недоступен")

    status = body.get("status")
    if status == "warning":
        sponsors = _sponsors(body)
        if sponsors:
            total = body.get("total_fixed_link")
            sponsor_total = total if isinstance(total, int) and total > 0 else len(sponsors)
            return AccessDecision(
                False,
                "warning",
                sponsors=sponsors,
                sponsor_total=max(sponsor_total, len(sponsors)),
                reason=_safe_text(body.get("message"), "Нужна подписка на каналы партнёров", 300),
            )
        logger.warning("Subgram returned warning without displayable sponsors; allowing access")
        return AccessDecision(True, "error", reason="Subgram не вернул доступные задания")
    if status == "ok":
        return AccessDecision(True, "ok", reason=_safe_text(body.get("message"), "Доступ подтверждён", 300))

    logger.warning("Subgram returned status=%r http=%s; allowing access", status, status_code)
    return AccessDecision(True, "error", reason=_safe_text(body.get("message"), "Ошибка Subgram", 300))

