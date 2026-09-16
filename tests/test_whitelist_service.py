"""Интеграционные тесты белого списка: БД + кэш + деградация при сбое Redis."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.db.models import GLOBAL_SCOPE  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.services.whitelist_service import WhitelistError, WhitelistService  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210
ADMIN_ID = 777


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        database_url="sqlite+aiosqlite:///:memory:",
        flibusta_bot_id=555001,
        flibusta_username="flibustafreebookbot",
        static_whitelist_ids="111,222",
        static_whitelist_usernames="@system_bot",
        super_admin_ids="777",
    )


async def make_service(redis: FakeRedis | None = None) -> WhitelistService:
    settings = make_settings()
    # Файловая БД в памяти живёт, пока жив пул: держим одно соединение.
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    return WhitelistService(settings, database, redis, local_ttl=0.0)


async def test_static_whitelist_and_flibusta() -> None:
    service = await make_service()
    assert service.is_flibusta(555001, None)
    assert service.is_flibusta(None, "FlibustaFreeBookBot")
    assert service.is_statically_trusted(111, None)
    assert service.is_statically_trusted(None, "@system_bot")
    assert not service.is_statically_trusted(999, "random_user")
    # Флибуста доверена без единого обращения к БД/Redis.
    assert await service.is_trusted(CHAT_ID, 555001, "flibustafreebookbot")


async def test_add_is_scoped_to_chat() -> None:
    service = await make_service()
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID, reason="книжный бот")
    assert await service.is_trusted(CHAT_ID, 42, None)
    assert not await service.is_trusted(OTHER_CHAT_ID, 42, None)


async def test_global_scope_applies_everywhere() -> None:
    service = await make_service()
    await service.add(chat_id=GLOBAL_SCOPE, username="@booksbot", added_by=ADMIN_ID)
    assert await service.is_trusted(CHAT_ID, 1, "booksbot")
    assert await service.is_trusted(OTHER_CHAT_ID, 2, "BooksBot")


async def test_remove_invalidates_cache() -> None:
    service = await make_service(FakeRedis())
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    assert await service.is_trusted(CHAT_ID, 42, None)
    removed = await service.remove(chat_id=CHAT_ID, user_id=42)
    assert removed == 1
    assert not await service.is_trusted(CHAT_ID, 42, None)


async def test_duplicate_add_is_rejected() -> None:
    service = await make_service()
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    try:
        await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    except WhitelistError:
        return
    raise AssertionError("повторное добавление должно отклоняться")


async def test_empty_target_is_rejected() -> None:
    service = await make_service()
    try:
        await service.add(chat_id=CHAT_ID, added_by=ADMIN_ID)
    except WhitelistError:
        return
    raise AssertionError("пустая цель должна отклоняться")


async def test_redis_cache_is_used_and_warmed() -> None:
    redis = FakeRedis()
    service = await make_service(redis)
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    assert await service.is_trusted(CHAT_ID, 42, None)
    # После первого чтения кэш прогрет: есть маркер готовности и множество id.
    assert any(key.endswith(":ready") for key in redis.store)
    assert any(key.endswith(f"wl:{CHAT_ID}:ids") for key in redis.store)


async def test_falls_back_to_db_when_redis_is_down() -> None:
    redis = FakeRedis(fail=True)
    service = await make_service(redis)
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    # Redis падает на каждой операции, но проверка обязана работать через БД.
    assert await service.is_trusted(CHAT_ID, 42, None)
    assert not await service.is_trusted(CHAT_ID, 43, None)


async def test_list_entries_includes_global() -> None:
    service = await make_service()
    await service.add(chat_id=CHAT_ID, user_id=42, added_by=ADMIN_ID)
    await service.add(chat_id=GLOBAL_SCOPE, username="booksbot", added_by=ADMIN_ID)
    entries = await service.list_entries(CHAT_ID)
    assert len(entries) == 2
    assert any(entry.is_global for entry in entries)


async def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            await func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - показываем любую поломку
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
