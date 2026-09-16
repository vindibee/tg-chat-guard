"""Инициализация async-движка и фабрики сессий SQLAlchemy 2.0."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.models import Base

logger = logging.getLogger(__name__)

__all__ = ["Database", "create_database"]


class Database:
    """Держатель движка и фабрики сессий."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    def session(self) -> AsyncSession:
        """Новая сессия (использовать как `async with db.session() as s:`)."""
        return self._session_factory()

    async def create_all(self) -> None:
        """Создаёт таблицы напрямую, минуя миграции.

        Используется только в тестах: там важна скорость, а схема всё равно
        одноразовая. Рабочий путь — `bot.db.migrator.run_migrations`.
        """
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("Схема БД синхронизирована", extra={"stage": "startup"})

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("Пул соединений с БД закрыт", extra={"stage": "shutdown"})


def create_database(dsn: str, *, echo: bool = False) -> Database:
    """Фабрика `Database` по DSN."""
    engine = create_async_engine(dsn, echo=echo, pool_pre_ping=True, future=True)
    return Database(engine)
