"""Durable short-lived Telegram conversation state."""

import json

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings


class InteractionStateStore:
    def __init__(self, redis: Redis | None = None, ttl_seconds: int = 3_600) -> None:
        self.redis = redis or Redis.from_url(get_settings().redis_url, decode_responses=True)
        self.ttl_seconds = ttl_seconds
        self.fallback: dict[int, dict] = {}

    @staticmethod
    def _key(telegram_id: int) -> str:
        return f"bot:interaction:{telegram_id}"

    async def set(self, telegram_id: int, kind: str, **data) -> None:
        value = {"kind": kind, **data}
        self.fallback[telegram_id] = value
        try:
            await self.redis.set(self._key(telegram_id), json.dumps(value, ensure_ascii=False), ex=self.ttl_seconds)
        except (RedisError, OSError):
            pass

    async def get(self, telegram_id: int) -> dict | None:
        try:
            raw = await self.redis.get(self._key(telegram_id))
        except (RedisError, OSError):
            return self.fallback.get(telegram_id)
        if not raw:
            return self.fallback.get(telegram_id)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            await self.clear(telegram_id)
            return None
        if isinstance(value, dict) and isinstance(value.get("kind"), str):
            self.fallback[telegram_id] = value
            return value
        return None

    async def clear(self, telegram_id: int) -> None:
        self.fallback.pop(telegram_id, None)
        try:
            await self.redis.delete(self._key(telegram_id))
        except (RedisError, OSError):
            pass

    async def close(self) -> None:
        try:
            await self.redis.aclose()
        except (RedisError, OSError):
            pass
