"""Реестр сообщений, удалённых модерацией.

Нужен книжному боту: Флибуста отвечает на запрос пользователя цитатой, и если
сам запрос оказался спамом и был удалён, в чате остаётся висеть ответ на
пустоту. Telegram не сообщает ботам, что сообщение удалили, поэтому помним
это сами.

Хранение в памяти процесса: ответ приходит через секунды после запроса, и
переживать перезапуск бота этим данным незачем.
"""

from __future__ import annotations

from bot.utils.ttl_cache import TTLCache

__all__ = ["DeletedMessageRegistry"]


class DeletedMessageRegistry:
    """Помнит, какие сообщения удалил бот, в пределах короткого окна."""

    def __init__(self, ttl: float = 3600.0, maxsize: int = 20_000) -> None:
        self._seen: TTLCache[tuple[int, int], bool] = TTLCache(ttl=ttl, maxsize=maxsize)

    def remember(self, chat_id: int, message_id: int) -> None:
        """Отмечает сообщение как удалённое модерацией."""
        self._seen.set((chat_id, message_id), True)

    def was_deleted(self, chat_id: int, message_id: int | None) -> bool:
        """Удаляли ли мы это сообщение (в пределах окна хранения)."""
        if message_id is None:
            return False
        return self._seen.get((chat_id, message_id)) is True

    def __len__(self) -> int:
        return len(self._seen)
