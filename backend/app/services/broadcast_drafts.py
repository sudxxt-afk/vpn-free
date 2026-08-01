import json
import uuid

from redis.asyncio import Redis

from app.config import get_settings


class BroadcastDraftStore:
    """Durable per-admin broadcast wizard state shared across bot restarts."""

    def __init__(self, redis: Redis | None = None, ttl_seconds: int = 86_400) -> None:
        self.redis = redis or Redis.from_url(get_settings().redis_url, decode_responses=True)
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(telegram_id: int) -> str:
        return f"bot:broadcast-draft:{telegram_id}"

    async def begin(self, telegram_id: int) -> dict:
        state = {
            "stage": "content",
            "client_request_id": str(uuid.uuid4()),
            "draft": {"text_html": "", "photo_file_id": None, "buttons": []},
        }
        await self.save(telegram_id, state)
        return state

    async def load(self, telegram_id: int) -> dict | None:
        raw = await self.redis.get(self._key(telegram_id))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            await self.clear(telegram_id)
            return None
        return value if isinstance(value, dict) else None

    async def save(self, telegram_id: int, state: dict) -> None:
        await self.redis.set(self._key(telegram_id), json.dumps(state, ensure_ascii=False), ex=self.ttl_seconds)

    async def clear(self, telegram_id: int) -> None:
        await self.redis.delete(self._key(telegram_id))

    async def close(self) -> None:
        await self.redis.aclose()
