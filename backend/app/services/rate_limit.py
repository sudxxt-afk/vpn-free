"""Redis-backed limits with a process-local safety net for Redis outages."""

import asyncio
import hashlib
import logging
import time

from fastapi import Request
from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
redis_client: Redis | None = None
_local_windows: dict[str, tuple[int, int]] = {}
_local_lock = asyncio.Lock()


async def _local_is_allowed(key: str, limit: int, window: int) -> tuple[bool, int, int]:
    """Keep enforcing a bounded limit when Redis is temporarily unavailable."""
    slot = int(time.monotonic() // window)
    async with _local_lock:
        previous_slot, count = _local_windows.get(key, (slot, 0))
        count = count + 1 if previous_slot == slot else 1
        _local_windows[key] = (slot, count)
        if len(_local_windows) > 10_000:
            stale = [item_key for item_key, (item_slot, _count) in _local_windows.items() if item_slot < slot]
            for item_key in stale:
                _local_windows.pop(item_key, None)
        return count <= limit, limit, window


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",", 1)[0].strip() if forwarded else (request.client.host if request.client else "unknown")


def rule_for(request: Request) -> tuple[str, int, int] | None:
    path = request.url.path
    if path in {"/health", "/docs", "/openapi.json"} or path.startswith("/admin"):
        return None
    ip = client_ip(request)
    if path == "/auth/login":
        return (f"login:{ip}", settings.login_rate_limit_per_15_minutes, 900)
    if path.startswith("/s/"):
        token = path.rsplit("/", 1)[-1]
        token_key = hashlib.sha256(token.encode()).hexdigest()[:16]
        return (f"subscription:{ip}:{token_key}", settings.subscription_rate_limit_per_minute, 60)
    if path.startswith("/internal/"):
        return (f"bot:{ip}", settings.api_rate_limit_per_minute, 60)
    return (f"api:{ip}", settings.api_rate_limit_per_minute, 60)


async def is_allowed(request: Request) -> tuple[bool, int, int] | None:
    rule = rule_for(request)
    if rule is None:
        return None
    key, limit, window = rule
    global redis_client
    try:
        if redis_client is None:
            redis_client = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        count = await redis_client.incr(f"ratelimit:{key}")
        if count == 1:
            await redis_client.expire(f"ratelimit:{key}", window)
        return count <= limit, limit, window
    except Exception as exc:
        logger.warning("Redis rate limiter unavailable; using local limiter: %s", exc.__class__.__name__)
        return await _local_is_allowed(key, limit, window)
