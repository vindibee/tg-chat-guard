"""Запуск миграций alembic из приложения.

Для личного бота отдельный шаг деплоя — лишняя церемония, поэтому схема
подтягивается на старте. Соединение передаётся в alembic через
``config.attributes``: второй пул к базе не открывается.

Отдельно разбирается случай «база уже есть, а alembic о ней не знает»:
такая база просто помечается текущей ревизией, вместо того чтобы падать на
повторном ``CREATE TABLE``.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect

from bot.db.models import Base
from bot.db.session import Database

logger = logging.getLogger(__name__)

__all__ = ["MigrationOutcome", "SchemaState", "run_migrations", "alembic_config"]

#: `bot/db/migrator.py` -> корень проекта.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Path = PROJECT_ROOT / "alembic.ini"


class SchemaState(StrEnum):
    """Что мы нашли в базе до миграции."""

    #: Пустая база — накатываем всё с нуля.
    FRESH = "fresh"
    #: Есть таблица alembic_version — обычный путь.
    MANAGED = "managed"
    #: Все таблицы моделей на месте, но alembic про них не знает.
    ADOPTABLE = "adoptable"
    #: Часть таблиц есть, часть нет — схема от старой версии.
    PARTIAL = "partial"


class MigrationOutcome(StrEnum):
    """Что мы сделали."""

    UPGRADED = "upgraded"
    ADOPTED = "adopted"


class SchemaMismatchError(RuntimeError):
    """База осталась от версии без миграций и не совпадает с моделями."""


def alembic_config(connection: Connection | None = None) -> Config:
    """Конфиг alembic, привязанный к переданному соединению."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # Логирование настраивает приложение; пусть alembic его не перебивает.
    config.attributes["configure_logger"] = False
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def inspect_state(connection: Connection) -> SchemaState:
    """Определяет, в каком виде застали базу."""
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" in tables:
        return SchemaState.MANAGED
    expected = set(Base.metadata.tables)
    if not tables & expected:
        return SchemaState.FRESH
    return SchemaState.ADOPTABLE if expected <= tables else SchemaState.PARTIAL


def _upgrade(connection: Connection) -> None:
    command.upgrade(alembic_config(connection), "head")


def _stamp(connection: Connection) -> None:
    command.stamp(alembic_config(connection), "head")


async def run_migrations(database: Database) -> MigrationOutcome:
    """Приводит схему к последней ревизии.

    Raises:
        SchemaMismatchError: база от старой версии без alembic — данные
            трогать вслепую нельзя, решение за человеком.
    """
    async with database.engine.begin() as connection:
        state = await connection.run_sync(inspect_state)

        if state is SchemaState.PARTIAL:
            raise SchemaMismatchError(
                "В базе есть таблицы от версии без миграций. Удалите файл базы "
                "(для SQLite) или выполните `alembic stamp head` вручную, "
                "предварительно убедившись, что схема совпадает с моделями."
            )

        if state is SchemaState.ADOPTABLE:
            await connection.run_sync(_stamp)
            logger.warning(
                "Существующая схема принята под управление alembic",
                extra={"stage": "startup", "component": "migrator"},
            )
            return MigrationOutcome.ADOPTED

        await connection.run_sync(_upgrade)
        logger.info(
            "Схема БД обновлена",
            extra={"stage": "startup", "component": "migrator", "schema_state": state.value},
        )
        return MigrationOutcome.UPGRADED
