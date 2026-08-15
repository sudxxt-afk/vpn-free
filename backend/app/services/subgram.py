"""Live Subgram sponsor access checks with strict access decisions."""

from dataclasses import dataclass
from datetime import date, timedelta
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
    ads_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SubscriptionReview:
    available: bool
    statuses: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class StatisticsPoint:
    label: str
    subscribers: int = 0
    revenue: float = 0.0
    average_price: float = 0.0


@dataclass(frozen=True)
class SubgramStatistics:
    configured: bool
    available: bool
    message: str
    total_subscribers: int = 0
    total_revenue: float = 0.0
    average_price: float = 0.0
    total_requests: int = 0
    successful_requests: int = 0
    days: tuple[StatisticsPoint, ...] = ()


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


def _ads_ids(body: dict) -> tuple[int, ...]:
    additional = body.get("additional")
    items = additional.get("sponsors") if isinstance(additional, dict) else None
    result: list[int] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                ads_id = int(item.get("ads_id"))
            except (TypeError, ValueError):
                continue
            if ads_id > 0 and ads_id not in result:
                result.append(ads_id)
    return tuple(result)


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


async def _statistics_request(params: dict) -> tuple[int, dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3)) as client:
        response = await client.get(f"{settings.subgram_base_url.rstrip('/')}/statistic", params=params)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


async def _subscriptions_request(payload: dict) -> tuple[int, dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
        response = await client.post(
            f"{settings.subgram_base_url.rstrip('/')}/get-user-subscriptions",
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
    """Grant access only after an explicit successful Subgram response."""
    settings = get_settings()
    if not settings.subgram_api_key:
        logger.error("Subgram access key is not configured; denying access")
        return AccessDecision(False, "error", reason="Проверка подписок временно не настроена. Попробуйте позже.")

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
        logger.warning("Subgram request failed; denying access: %s", exc.__class__.__name__)
        return AccessDecision(False, "error", reason="Subgram временно недоступен. Попробуйте снова через минуту.")

    status = body.get("status")
    ads_ids = _ads_ids(body)
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
                ads_ids=ads_ids,
            )
        logger.warning("Subgram returned warning without displayable sponsors; denying access")
        return AccessDecision(False, "error", reason="Subgram не вернул доступные задания. Попробуйте снова через минуту.")
    if status == "ok":
        return AccessDecision(True, "ok", reason=_safe_text(body.get("message"), "Доступ подтверждён", 300), ads_ids=ads_ids)

    logger.warning("Subgram returned status=%r http=%s; denying access", status, status_code)
    return AccessDecision(False, "error", reason=_safe_text(body.get("message"), "Ошибка проверки Subgram. Попробуйте позже.", 300))


async def get_subgram_subscriptions(user_id: int, ads_ids: tuple[int, ...]) -> SubscriptionReview:
    """Recheck already assigned sponsors without requesting a new task set."""
    settings = get_settings()
    if not settings.subgram_api_key or not ads_ids:
        return SubscriptionReview(False)
    try:
        status_code, body = await _subscriptions_request({"user_id": user_id, "ads_ids": list(ads_ids)})
    except httpx.HTTPError as exc:
        logger.warning("Subgram subscription recheck failed: %s", exc.__class__.__name__)
        return SubscriptionReview(False)
    if status_code != 200 or body.get("status") != "ok":
        logger.warning("Subgram subscription recheck rejected http=%s status=%r", status_code, body.get("status"))
        return SubscriptionReview(False)
    additional = body.get("additional")
    items = additional.get("sponsors") if isinstance(additional, dict) else None
    statuses: list[tuple[int, str]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                ads_id = int(item.get("ads_id"))
            except (TypeError, ValueError):
                continue
            status = item.get("status")
            if ads_id in ads_ids and status in {"subscribed", "notgetted", "unsubscribed"}:
                statuses.append((ads_id, status))
    return SubscriptionReview(True, tuple(statuses))


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return round(max(0.0, float(value)), 2)
    except (TypeError, ValueError):
        return 0.0


def _statistic_points(data: dict) -> tuple[StatisticsPoint, ...]:
    labels = data.get("labels") if isinstance(data.get("labels"), list) else []
    subscribers = data.get("subscribers_data") if isinstance(data.get("subscribers_data"), list) else []
    revenue = data.get("value_data") if isinstance(data.get("value_data"), list) else []
    average_prices = data.get("avg_price_data") if isinstance(data.get("avg_price_data"), list) else []
    return tuple(
        StatisticsPoint(
            label=_safe_text(label, "—", 32),
            subscribers=_as_int(subscribers[index] if index < len(subscribers) else 0),
            revenue=_as_float(revenue[index] if index < len(revenue) else 0),
            average_price=_as_float(average_prices[index] if index < len(average_prices) else 0),
        )
        for index, label in enumerate(labels)
    )


async def get_subgram_statistics(days: int = 14) -> SubgramStatistics:
    """Fetch publisher revenue analytics using Subgram's dedicated statistics token."""
    settings = get_settings()
    token = settings.subgram_statistics_token.strip()
    if not token:
        return SubgramStatistics(
            configured=False,
            available=False,
            message="Добавьте API Token статистики Subgram в SUBGRAM_STATISTICS_TOKEN",
        )

    days = max(1, min(days, 90))
    end = date.today()
    raw_bot_id = str(settings.subgram_statistics_bot_id or "").strip()
    if not raw_bot_id:
        raw_bot_id = str(getattr(settings, "telegram_bot_token", "")).partition(":")[0]
    bot_id = int(raw_bot_id) if raw_bot_id.isdigit() else None
    params: dict[str, object] = {
        "api_token": token,
        "action": "bots" if bot_id else "allbots",
        "start_date": str(end - timedelta(days=days - 1)),
        "end_date": str(end),
        "output_format": "json",
    }
    if bot_id:
        params["bot_id"] = bot_id

    try:
        status_code, body = await _statistics_request(params)
    except httpx.HTTPError as exc:
        logger.warning("Subgram statistics request failed: %s", exc.__class__.__name__)
        return SubgramStatistics(True, False, "Subgram временно недоступен")

    if not isinstance(body, dict) or body.get("status") != "ok":
        message = body.get("message") if isinstance(body, dict) else None
        logger.warning("Subgram statistics rejected request with http=%s", status_code)
        return SubgramStatistics(True, False, _safe_text(message, "Не удалось получить статистику Subgram", 300))

    data = body.get("data")
    if not isinstance(data, dict):
        return SubgramStatistics(True, False, "Subgram вернул статистику без данных")
    points = _statistic_points(data)
    total_subscribers = _as_int(data.get("total_subscribers")) or sum(item.subscribers for item in points)
    total_revenue = _as_float(data.get("total_value")) or round(sum(item.revenue for item in points), 2)
    average_price = round(total_revenue / total_subscribers, 2) if total_subscribers else 0.0
    request_stats = data.get("requests_stats") if isinstance(data.get("requests_stats"), dict) else {}
    return SubgramStatistics(
        configured=True,
        available=True,
        message=_safe_text(body.get("message"), "Статистика получена", 300),
        total_subscribers=total_subscribers,
        total_revenue=total_revenue,
        average_price=average_price,
        total_requests=_as_int(request_stats.get("total_requests")),
        successful_requests=_as_int(request_stats.get("successful_requests")),
        days=points,
    )
