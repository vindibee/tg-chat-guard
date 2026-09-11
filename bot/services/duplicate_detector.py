"""Поиск одинаковых сообщений, разосланных разными участниками.

Зачем: самая неудобная для словаря рассылка — та, где нет ни ссылок, ни
стоп-слов. «Здравствуйте, ищу людей для сотрудничества» с пяти свежих аккаунтов
за час — это рассылка, и видно это только по повторению.

Считаем **разных авторов**, а не число повторов: человек, поднимающий свой
книжный запрос второй раз за вечер, не должен от этого пострадать.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot.services.text_cleaner import TextCleaner
from bot.utils.ttl_cache import TTLCache

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["DuplicateDetector"]


class DuplicateDetector:
    """Считает, сколько разных участников прислали один и тот же текст.

    При недоступности Redis счёт идёт в памяти процесса: для одного инстанса
    поведение не меняется, теряется только общий счёт между репликами.
    """

    def __init__(self, settings: Settings, redis: Redis | None = None) -> None:
        self._settings = settings
        self._redis = redis
        self._local: TTLCache[tuple[int, str], set[int]] = TTLCache(
            ttl=float(settings.duplicate_window), maxsize=20_000
        )

    @property
    def enabled(self) -> bool:
        return self._settings.duplicate_enabled

    def _key(self, chat_id: int, fingerprint: str) -> str:
        return self._settings.redis_key("dup", chat_id, fingerprint)

    def _too_short(self, text: str) -> bool:
        """Короткие реплики совпадают у всех и дубликатами не считаются."""
        return len(text.strip()) < self._settings.duplicate_min_length

    async def register(self, chat_id: int, user_id: int, text: str) -> int:
        """Учитывает сообщение и возвращает число разных авторов этого текста.

        Возвращает 0, если проверка отключена, текст слишком короткий или автор
        неизвестен — вызывающему коду достаточно сравнить результат с порогом.
        """
        if not self.enabled or not user_id or self._too_short(text):
            return 0

        fingerprint = TextCleaner.fingerprint(text)
        if self._redis is None:
            return self._register_locally(chat_id, fingerprint, user_id)

        key = self._key(chat_id, fingerprint)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.sadd(key, user_id)
                pipe.expire(key, self._settings.duplicate_window)
                pipe.scard(key)
                _, _, authors = await pipe.execute()
            return int(authors)
        except Exception as exc:
            logger.debug("Redis недоступен, считаем дубликаты в памяти: %s", exc)
            return self._register_locally(chat_id, fingerprint, user_id)

    def _register_locally(self, chat_id: int, fingerprint: str, user_id: int) -> int:
        authors = self._local.setdefault((chat_id, fingerprint), set())
        authors.add(user_id)
        return len(authors)

    def is_duplicate(self, authors: int) -> bool:
        """Достаточно ли разных авторов, чтобы считать текст рассылкой."""
        return authors >= self._settings.duplicate_threshold
