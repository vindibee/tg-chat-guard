"""Жалобы участников: подсчёт уникальных репортов на сообщение."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot.utils.ttl_cache import TTLCache

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["ReportService"]


class ReportService:
    """Считает, сколько разных участников пожаловались на одно сообщение.

    Если Redis не настроен или недоступен, счёт идёт в памяти процесса: порог
    и защита от повторных жалоб продолжают работать, теряется только общий
    счёт между несколькими инстансами бота.
    """

    #: Жалобы живут сутки.
    TTL: int = 60 * 60 * 24

    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self._settings = settings
        self._redis = redis
        self._local: TTLCache[tuple[int, int], set[int]] = TTLCache(ttl=self.TTL, maxsize=5_000)

    def _key(self, chat_id: int, message_id: int) -> str:
        return self._settings.redis_key("report", chat_id, message_id)

    async def register(self, chat_id: int, message_id: int, reporter_id: int) -> int:
        """Регистрирует жалобу. Возвращает число уникальных жалоб (0 — повтор).

        Повторная жалоба того же участника не увеличивает счётчик.
        """
        if self._redis is None:
            return self._register_locally(chat_id, message_id, reporter_id)
        key = self._key(chat_id, message_id)
        try:
            added = await self._redis.sadd(key, reporter_id)
            if not added:
                return 0
            await self._redis.expire(key, self.TTL)
            return int(await self._redis.scard(key))
        except Exception as exc:
            logger.debug("Redis недоступен, считаем жалобы в памяти: %s", exc)
            return self._register_locally(chat_id, message_id, reporter_id)

    def _register_locally(self, chat_id: int, message_id: int, reporter_id: int) -> int:
        """Запасной счётчик в памяти процесса."""
        reporters = self._local.setdefault((chat_id, message_id), set())
        if reporter_id in reporters:
            return 0
        reporters.add(reporter_id)
        return len(reporters)

    def is_enough(self, count: int) -> bool:
        """Достигнут ли порог вызова администраторов."""
        return count >= self._settings.report_threshold
