"""Двухуровневый белый список: статический (.env) + динамический (Redis/БД).

Схема доступа к данным:

    локальный снимок в памяти (TTL ~30 с)
        └─> Redis (общий кэш для всех инстансов бота)
                └─> PostgreSQL/SQLite (источник истины)

Любая ошибка Redis не ломает модерацию — сервис прозрачно уходит в БД
и продолжает работать (fail-open по кэшу, fail-safe по данным).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from bot.db.models import GLOBAL_SCOPE, WhitelistEntry
from bot.db.session import Database

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

    from bot.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "WhitelistService",
    "WhitelistSnapshot",
    "WhitelistError",
    "normalize_username",
]


class WhitelistError(RuntimeError):
    """Ошибка бизнес-уровня белого списка (показывается администратору)."""


def normalize_username(username: str | None) -> str:
    """Приводит username к канонической форме: без `@`, в нижнем регистре."""
    return (username or "").lstrip("@").strip().lower()


@dataclass(frozen=True, slots=True)
class WhitelistSnapshot:
    """Срез белого списка одной области видимости (чат или глобально)."""

    ids: frozenset[int]
    usernames: frozenset[str]

    def contains(self, user_id: int | None, username: str | None) -> bool:
        if user_id is not None and user_id in self.ids:
            return True
        name = normalize_username(username)
        return bool(name) and name in self.usernames

    @property
    def is_empty(self) -> bool:
        return not self.ids and not self.usernames


class WhitelistService:
    """Сервис проверки и управления белым списком."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        redis: Redis | None = None,
        *,
        local_ttl: float = 30.0,
    ) -> None:
        self._settings = settings
        self._db = database
        self._redis = redis
        self._local_ttl = local_ttl
        #: scope -> (истекает_в, снимок)
        self._local: dict[int, tuple[float, WhitelistSnapshot]] = {}

    # ------------------------------------------------------------------ #
    #                             ПРОВЕРКА                                #
    # ------------------------------------------------------------------ #

    def is_statically_trusted(self, user_id: int | None, username: str | None) -> bool:
        """Проверка по неизменяемому списку из `.env` (в т.ч. бот Флибусты)."""
        if user_id is not None and user_id in self._settings.protected_ids:
            return True
        name = normalize_username(username)
        return bool(name) and name in self._settings.protected_usernames

    def is_flibusta(self, user_id: int | None, username: str | None) -> bool:
        """Отдельная проверка книжного бота — он полностью вне модерации."""
        if user_id is not None and self._settings.flibusta_bot_id == user_id:
            return True
        name = normalize_username(username)
        return bool(name) and name == self._settings.flibusta_username

    async def is_trusted(
        self, chat_id: int, user_id: int | None, username: str | None = None
    ) -> bool:
        """Итоговая проверка: статический список -> глобальный -> список чата."""
        if self.is_statically_trusted(user_id, username):
            return True
        global_snapshot = await self.snapshot(GLOBAL_SCOPE)
        if global_snapshot.contains(user_id, username):
            return True
        chat_snapshot = await self.snapshot(chat_id)
        return chat_snapshot.contains(user_id, username)

    async def snapshot(self, scope: int) -> WhitelistSnapshot:
        """Срез белого списка с кэшированием на двух уровнях."""
        cached = self._local.get(scope)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]

        snapshot = await self._from_redis(scope)
        if snapshot is None:
            snapshot = await self._from_database(scope)
            await self._fill_redis(scope, snapshot)

        self._local[scope] = (now + self._local_ttl, snapshot)
        return snapshot

    # ------------------------------------------------------------------ #
    #                            ИСТОЧНИКИ                                #
    # ------------------------------------------------------------------ #

    def _keys(self, scope: int) -> tuple[str, str, str]:
        """Ключи Redis: множество id, множество username, маркер прогрева."""
        return (
            self._settings.redis_key("wl", scope, "ids"),
            self._settings.redis_key("wl", scope, "names"),
            self._settings.redis_key("wl", scope, "ready"),
        )

    async def _from_redis(self, scope: int) -> WhitelistSnapshot | None:
        """Читает срез из Redis. `None` — кэш холодный или Redis недоступен."""
        if self._redis is None:
            return None
        ids_key, names_key, ready_key = self._keys(scope)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.exists(ready_key)
                pipe.smembers(ids_key)
                pipe.smembers(names_key)
                ready, raw_ids, raw_names = await pipe.execute()
        except Exception as exc:  # redis.RedisError и сетевые сбои
            logger.warning(
                "Redis недоступен, читаем белый список из БД: %s",
                exc,
                extra={"scope": scope, "component": "whitelist"},
            )
            return None
        if not ready:
            return None
        return WhitelistSnapshot(
            ids=frozenset(int(v) for v in self._decode(raw_ids) if v.isdigit()),
            usernames=frozenset(v.lower() for v in self._decode(raw_names)),
        )

    async def _fill_redis(self, scope: int, snapshot: WhitelistSnapshot) -> None:
        """Прогревает Redis срезом из БД."""
        if self._redis is None:
            return
        ids_key, names_key, ready_key = self._keys(scope)
        ttl = self._settings.whitelist_cache_ttl
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(ids_key, names_key)
                if snapshot.ids:
                    pipe.sadd(ids_key, *snapshot.ids)
                    pipe.expire(ids_key, ttl)
                if snapshot.usernames:
                    pipe.sadd(names_key, *snapshot.usernames)
                    pipe.expire(names_key, ttl)
                pipe.set(ready_key, "1", ex=ttl)
                await pipe.execute()
        except Exception as exc:
            logger.warning(
                "Не удалось прогреть кэш белого списка: %s",
                exc,
                extra={"scope": scope, "component": "whitelist"},
            )

    async def _from_database(self, scope: int) -> WhitelistSnapshot:
        """Источник истины. При сбое БД — пустой срез, модерация продолжает работу."""
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(WhitelistEntry.user_id, WhitelistEntry.username).where(
                        WhitelistEntry.chat_id == scope
                    )
                )
                rows = result.all()
        except SQLAlchemyError as exc:
            logger.error(
                "Ошибка чтения белого списка из БД: %s",
                exc,
                exc_info=True,
                extra={"scope": scope, "component": "whitelist"},
            )
            return WhitelistSnapshot(frozenset(), frozenset())
        return WhitelistSnapshot(
            ids=frozenset(user_id for user_id, _ in rows if user_id),
            usernames=frozenset(name.lower() for _, name in rows if name),
        )

    @staticmethod
    def _decode(values: Iterable[bytes | str]) -> list[str]:
        """Redis может отдавать и bytes, и str (зависит от decode_responses)."""
        return [
            value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)
            for value in values
        ]

    # ------------------------------------------------------------------ #
    #                            УПРАВЛЕНИЕ                               #
    # ------------------------------------------------------------------ #

    async def add(
        self,
        *,
        chat_id: int,
        user_id: int | None = None,
        username: str | None = None,
        added_by: int,
        reason: str | None = None,
        is_bot: bool = False,
    ) -> WhitelistEntry:
        """Добавляет пользователя/бота в белый список.

        Raises:
            WhitelistError: не передан ни id, ни username, либо запись уже есть.
        """
        normalized_name = normalize_username(username)
        target_id = user_id or 0
        if not target_id and not normalized_name:
            raise WhitelistError("Нужно указать user_id или @username.")

        entry = WhitelistEntry(
            chat_id=chat_id,
            user_id=target_id,
            username=normalized_name,
            is_bot=is_bot,
            added_by=added_by,
            reason=(reason or None),
        )
        try:
            async with self._db.session() as session:
                session.add(entry)
                await session.commit()
        except IntegrityError:
            raise WhitelistError("Эта запись уже есть в белом списке.") from None
        except SQLAlchemyError as exc:
            logger.error("Ошибка записи в белый список: %s", exc, exc_info=True)
            raise WhitelistError("Не удалось сохранить запись — попробуйте позже.") from exc

        await self.invalidate(chat_id)
        logger.info(
            "Белый список пополнен",
            extra={
                "component": "whitelist",
                "chat_id": chat_id,
                "target_id": target_id,
                "target_username": normalized_name,
                "added_by": added_by,
            },
        )
        return entry

    async def remove(
        self, *, chat_id: int, user_id: int | None = None, username: str | None = None
    ) -> int:
        """Удаляет записи по id или username. Возвращает количество удалённых."""
        normalized_name = normalize_username(username)
        if not user_id and not normalized_name:
            raise WhitelistError("Нужно указать user_id или @username.")

        statement = delete(WhitelistEntry).where(WhitelistEntry.chat_id == chat_id)
        if user_id:
            statement = statement.where(WhitelistEntry.user_id == user_id)
        else:
            statement = statement.where(WhitelistEntry.username == normalized_name)

        try:
            async with self._db.session() as session:
                result = await session.execute(statement)
                await session.commit()
                removed = int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            logger.error("Ошибка удаления из белого списка: %s", exc, exc_info=True)
            raise WhitelistError("Не удалось удалить запись — попробуйте позже.") from exc

        await self.invalidate(chat_id)
        logger.info(
            "Записи удалены из белого списка",
            extra={
                "component": "whitelist",
                "chat_id": chat_id,
                "removed": removed,
                "target_id": user_id or 0,
                "target_username": normalized_name,
            },
        )
        return removed

    async def list_entries(
        self, chat_id: int, *, include_global: bool = True, limit: int = 100
    ) -> Sequence[WhitelistEntry]:
        """Записи чата (и глобальные) для админского вывода."""
        scopes = [chat_id, GLOBAL_SCOPE] if include_global else [chat_id]
        try:
            async with self._db.session() as session:
                result = await session.execute(
                    select(WhitelistEntry)
                    .where(WhitelistEntry.chat_id.in_(scopes))
                    .order_by(WhitelistEntry.chat_id, WhitelistEntry.id)
                    .limit(limit)
                )
                return result.scalars().all()
        except SQLAlchemyError as exc:
            logger.error("Ошибка чтения белого списка: %s", exc, exc_info=True)
            raise WhitelistError("БД недоступна — попробуйте позже.") from exc

    async def invalidate(self, scope: int) -> None:
        """Сбрасывает кэш обоих уровней для области видимости.

        Note:
            Локальные снимки других инстансов протухнут сами не позднее `local_ttl`.
        """
        self._local.pop(scope, None)
        if self._redis is None:
            return
        try:
            await self._redis.delete(*self._keys(scope))
        except Exception as exc:
            logger.warning("Не удалось сбросить кэш белого списка: %s", exc)

    async def warmup(self, chat_ids: Iterable[int] = ()) -> None:
        """Прогрев кэша на старте: глобальный список и указанные чаты."""
        for scope in (GLOBAL_SCOPE, *chat_ids):
            await self.snapshot(scope)
