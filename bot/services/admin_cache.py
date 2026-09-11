"""Кэш администраторов чата: экономит вызовы Bot API на каждом сообщении."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot

logger = logging.getLogger(__name__)

__all__ = ["ChatAdminCache"]


class ChatAdminCache:
    """TTL-кэш списка админов чата.

    Telegram не присылает событий о смене прав, поэтому кэш обновляется по TTL
    (по умолчанию 5 минут) и принудительно — через `invalidate`.
    """

    def __init__(self, ttl: float = 300.0) -> None:
        self._ttl = ttl
        self._cache: dict[int, tuple[float, frozenset[int]]] = {}

    async def get_admin_ids(self, bot: Bot, chat_id: int) -> frozenset[int]:
        """Возвращает id администраторов чата (включая владельца)."""
        now = time.monotonic()
        cached = self._cache.get(chat_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            members = await bot.get_chat_administrators(chat_id)
        except TelegramAPIError as exc:
            logger.warning(
                "Не удалось получить админов чата: %s",
                exc,
                extra={"chat_id": chat_id, "component": "admin_cache"},
            )
            # Лучше отдать протухшие данные, чем ошибочно лишить админа прав.
            return cached[1] if cached is not None else frozenset()

        admin_ids = frozenset(member.user.id for member in members)
        self._cache[chat_id] = (now + self._ttl, admin_ids)
        return admin_ids

    async def is_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        return user_id in await self.get_admin_ids(bot, chat_id)

    def invalidate(self, chat_id: int | None = None) -> None:
        """Сбрасывает кэш конкретного чата или полностью."""
        if chat_id is None:
            self._cache.clear()
        else:
            self._cache.pop(chat_id, None)
