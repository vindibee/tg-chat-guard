"""Память о том, какого бота рекламировал каждый участник.

Зачем: спам-сеть постит в чат один и тот же бот раз в 40 минут, каждый раз
меняя текст — «videos bot= @x», «check BEST vide0s … @x», «bhai log … @x».
Словарь за такой ротацией не успевает, а детектор дубликатов её не видит:
тексты разные, автор один. Постоянен только бот — его и считаем.

Считаем пару «автор + бот» в конкретном чате, а не просто бота: участник,
посоветовавший книжного бота, не страдает от того, что его советовали другие.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from bot.utils.ttl_cache import TTLCache

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["PromoTracker"]


class PromoTracker:
    """Считает, сколько раз автор упоминал одного и того же бота.

    При недоступности Redis счёт идёт в памяти процесса — как у детектора
    дубликатов: теряется только общий счёт между репликами и перезапусками.
    """

    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self._settings = settings
        self._redis = redis
        self._local: TTLCache[tuple[int, int, str], int] = TTLCache(
            ttl=float(settings.promo_repeat_window), maxsize=20_000
        )

    def _key(self, chat_id: int, user_id: int, target: str) -> str:
        return self._settings.redis_key("promo", chat_id, user_id, target)

    async def register(self, chat_id: int, user_id: int, targets: Iterable[str]) -> int:
        """Учитывает упоминания и возвращает число ПРЕДЫДУЩИХ упоминаний.

        Если в сообщении несколько ботов, берётся самый «заезженный»:
        0 — бот встречается у автора впервые, 1 — это второй раз и так далее.
        """
        unique = sorted(set(targets))
        if not user_id or not unique:
            return 0
        if self._redis is None:
            return self._register_locally(chat_id, user_id, unique)

        window = self._settings.promo_repeat_window
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for target in unique:
                    key = self._key(chat_id, user_id, target)
                    pipe.incr(key)
                    pipe.expire(key, window)
                results = await pipe.execute()
        except Exception as exc:
            logger.debug("Redis недоступен, считаем рекламу ботов в памяти: %s", exc)
            return self._register_locally(chat_id, user_id, unique)
        counts = [int(value) for value in results[::2]]
        return max(counts) - 1

    def _register_locally(self, chat_id: int, user_id: int, targets: list[str]) -> int:
        previous = 0
        for target in targets:
            key = (chat_id, user_id, target)
            count = (self._local.get(key) or 0) + 1
            self._local.set(key, count)
            previous = max(previous, count - 1)
        return previous
