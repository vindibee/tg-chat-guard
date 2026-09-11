"""Окружение alembic: URL берём из настроек бота, схему — из моделей.

Поддерживаются три режима:

* обычный ``alembic upgrade head`` — создаёт свой async-движок;
* ``alembic upgrade head --sql`` (offline) — печатает SQL;
* запуск из самого бота: соединение передаётся через
  ``config.attributes["connection"]``, чтобы не открывать второй пул.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from bot.config import get_settings
from bot.db.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

#: Автогенерация сравнивает БД именно с этими моделями.
target_metadata = Base.metadata


def database_url() -> str:
    """URL из `.env`; значение в alembic.ini остаётся плейсхолдером."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Режим --sql: генерируем скрипт без подключения к базе."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite не умеет ALTER COLUMN — правки типов идут через временную таблицу.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Создаёт собственный движок — путь для командной строки."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # Соединение пришло из приложения — второй пул не нужен.
        do_run_migrations(connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
