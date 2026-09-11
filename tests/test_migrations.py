"""Тесты миграций: накат, приём существующей схемы и совпадение с моделями."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import Connection, inspect  # noqa: E402

from bot.db.migrator import (  # noqa: E402
    MigrationOutcome,
    SchemaMismatchError,
    SchemaState,
    inspect_state,
    run_migrations,
)
from bot.db.models import Base  # noqa: E402
from bot.db.session import Database, create_database  # noqa: E402


def make_database(tmp_path: Path, name: str = "test.db") -> Database:
    """Файловая БД: миграции живут дольше одного соединения."""
    return create_database(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")


async def table_names(database: Database) -> set[str]:
    async with database.engine.connect() as connection:
        return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))


async def test_fresh_database_gets_full_schema(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        assert await run_migrations(database) is MigrationOutcome.UPGRADED
        tables = await table_names(database)
        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables
    finally:
        await database.dispose()


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        await run_migrations(database)
        assert await run_migrations(database) is MigrationOutcome.UPGRADED
    finally:
        await database.dispose()


async def test_existing_schema_is_adopted(tmp_path: Path) -> None:
    """База, созданная старым `create_all()`, не должна ронять бота."""
    database = make_database(tmp_path, "legacy.db")
    try:
        await database.create_all()
        assert await run_migrations(database) is MigrationOutcome.ADOPTED
        assert "alembic_version" in await table_names(database)
    finally:
        await database.dispose()


async def test_partial_schema_is_rejected(tmp_path: Path) -> None:
    """Схема от старой версии — решение за человеком, вслепую не трогаем."""
    database = make_database(tmp_path, "partial.db")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(
                lambda c: Base.metadata.tables["whitelist_entries"].create(c)
            )
            state = await connection.run_sync(inspect_state)
        assert state is SchemaState.PARTIAL

        try:
            await run_migrations(database)
        except SchemaMismatchError as exc:
            assert "alembic stamp head" in str(exc)
            return
        raise AssertionError("неполная схема должна отклоняться")
    finally:
        await database.dispose()


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "target_metadata": Base.metadata}
    )
    return list(compare_metadata(context, Base.metadata))


async def test_migrations_match_models(tmp_path: Path) -> None:
    """Ключевая регрессия: после наката автогенерация не находит отличий.

    Если модель изменили, а миграцию не добавили — тест это покажет.
    """
    database = make_database(tmp_path, "diff.db")
    try:
        await run_migrations(database)
        async with database.engine.connect() as connection:
            differences = await connection.run_sync(_diff)
        assert differences == [], f"модели разошлись с миграциями: {differences}"
    finally:
        await database.dispose()


async def test_schema_states_are_detected(tmp_path: Path) -> None:
    database = make_database(tmp_path, "states.db")
    try:
        async with database.engine.begin() as connection:
            assert await connection.run_sync(inspect_state) is SchemaState.FRESH
        await run_migrations(database)
        async with database.engine.begin() as connection:
            assert await connection.run_sync(inspect_state) is SchemaState.MANAGED
    finally:
        await database.dispose()


async def _run() -> int:
    import tempfile

    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            try:
                await func(Path(tmp))
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
