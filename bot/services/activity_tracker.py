"""Учёт активности участников и статус «одобренного».

Логика счётчика:

* растёт **только на чистых сообщениях** — спамер, написавший десяток «привет»,
  не получает пропуск (наивный счётчик всех сообщений именно так и ломается);
* срабатывание антиспама обнуляет прогресс;
* пока сообщений мало — участник считается новичком и получает добавку к скору;
* после `approved_after_messages` чистых сообщений участник «одобрен» и не
  проверяется вовсе (кроме режима `paranoid_mode`).

При недоступности Redis счётчик всегда равен нулю: никто не одобрен, проверяются
все. Это шумнее, но безопаснее обратного поведения.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["ActivityTracker"]


class ActivityTracker:
    """Счётчик чистых сообщений участника в конкретном чате."""

    #: Время жизни счётчика (сек.) — 90 дней.
    COUNTER_TTL: int = 60 * 60 * 24 * 90

    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self._settings = settings
        self._redis = redis

    def _key(self, chat_id: int, user_id: int) -> str:
        return self._settings.redis_key("activity", chat_id, user_id)

    # ------------------------------------------------------------------ #
    #                              ЧТЕНИЕ                                 #
    # ------------------------------------------------------------------ #

    async def get_count(self, chat_id: int, user_id: int) -> int:
        """Сколько чистых сообщений написал участник (0 при сбое Redis)."""
        if self._redis is None:
            return 0
        key = self._key(chat_id, user_id)
        try:
            # Заодно продлеваем TTL: иначе счётчик одобренного участника
            # протухнет через COUNTER_TTL, и его снова начнут проверять,
            # хотя он всё это время активно писал.
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.get(key)
                pipe.expire(key, self.COUNTER_TTL)
                value, _ = await pipe.execute()
        except Exception as exc:
            logger.debug("Не удалось прочитать счётчик активности: %s", exc)
            return 0
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    def is_approved(self, count: int) -> bool:
        """Участник набрал достаточно чистых сообщений и больше не проверяется."""
        if self._settings.paranoid_mode:
            return False
        return count >= self._settings.approved_after_messages

    def is_new_member(self, count: int) -> bool:
        """Участник ещё в «карантинной» зоне — его сообщения весят больше."""
        return count < self._settings.new_member_messages

    # ------------------------------------------------------------------ #
    #                              ЗАПИСЬ                                 #
    # ------------------------------------------------------------------ #

    async def register_clean_message(self, chat_id: int, user_id: int) -> int:
        """Засчитывает чистое сообщение. Возвращает новое значение счётчика."""
        if self._redis is None:
            return 0
        key = self._key(chat_id, user_id)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.incr(key)
                pipe.expire(key, self.COUNTER_TTL)
                count, _ = await pipe.execute()
            return int(count)
        except Exception as exc:
            logger.debug("Не удалось обновить счётчик активности: %s", exc)
            return 0

    async def reset(self, chat_id: int, user_id: int) -> None:
        """Обнуляет прогресс — вызывается при срабатывании антиспама."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(chat_id, user_id))
        except Exception as exc:
            logger.debug("Не удалось сбросить счётчик активности: %s", exc)
