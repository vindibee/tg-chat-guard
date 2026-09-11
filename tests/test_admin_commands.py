"""Тесты админских команд белого списка и отладки правил."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.filters import CommandObject  # noqa: E402
from aiogram.types import Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.admin_whitelist import (  # noqa: E402
    antispam_status,
    show_help,
    spam_check,
    whitelist_add,
    whitelist_list,
    whitelist_remove,
)
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig  # noqa: E402
from bot.services.whitelist_service import WhitelistService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_ID = 501
TARGET_ID = 999


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "bot_token": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "super_admin_ids": str(ADMIN_ID),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_message(bot: FakeBot, text: str = "/cmd", reply: Message | None = None) -> Message:
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="Админ"),
        text=text,
        reply_to_message=reply,
    )
    return message.as_(bot)


def make_reply(bot: FakeBot, text: str, name: str = "Спамер") -> Message:
    message = Message(
        message_id=2,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=TARGET_ID, is_bot=False, first_name=name, username="spammer"),
        text=text,
    )
    return message.as_(bot)


def command(args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command="cmd", args=args)


async def make_env() -> tuple[Settings, WhitelistService, FakeBot]:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    return settings, WhitelistService(settings, database, None, local_ttl=0.0), FakeBot()


def answer(bot: FakeBot) -> str:
    """Текст первого ответа бота."""
    return bot.payload("SendMessage").get("text", "")


def last_answer(bot: FakeBot) -> str:
    """Текст последнего ответа — когда в тесте несколько вызовов подряд."""
    for call, payload in reversed(bot.calls):
        if call == "SendMessage":
            return payload.get("text", "")
    return ""


# ------------------------------ /whitelist_add -----------------------------


async def test_add_by_username_works() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist_add(
        message=make_message(bot),
        command=command("@booksbot доверенный поисковик"),
        settings=settings,
        whitelist=whitelist,
    )
    assert "добавлен в белый список" in answer(bot)
    assert await whitelist.is_trusted(CHAT_ID, 0, "booksbot")


async def test_add_by_reply_takes_author() -> None:
    settings, whitelist, bot = await make_env()
    reply = make_reply(bot, "обычное сообщение")
    await whitelist_add(
        message=make_message(bot, reply=reply),
        command=command(None),
        settings=settings,
        whitelist=whitelist,
    )
    assert await whitelist.is_trusted(CHAT_ID, TARGET_ID, "spammer")


async def test_add_rejects_garbage() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist_add(
        message=make_message(bot),
        command=command("@пользователь"),
        settings=settings,
        whitelist=whitelist,
    )
    assert "Некорректный username" in answer(bot)


async def test_duplicate_add_is_reported() -> None:
    settings, whitelist, bot = await make_env()
    for _ in range(2):
        await whitelist_add(
            message=make_message(bot),
            command=command("@booksbot"),
            settings=settings,
            whitelist=whitelist,
        )
    assert "уже есть" in last_answer(bot)


# ---------------------------- /whitelist_remove ----------------------------


async def test_remove_deletes_entry() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist.add(chat_id=CHAT_ID, username="booksbot", added_by=ADMIN_ID)
    await whitelist_remove(
        message=make_message(bot),
        command=command("@booksbot"),
        settings=settings,
        whitelist=whitelist,
    )
    assert "удалён из белого списка" in answer(bot)
    assert not await whitelist.is_trusted(CHAT_ID, 0, "booksbot")


async def test_remove_reports_missing_entry() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist_remove(
        message=make_message(bot),
        command=command("@nobody"),
        settings=settings,
        whitelist=whitelist,
    )
    assert "не найден" in answer(bot)


# ----------------------------- /whitelist_list -----------------------------


async def test_list_shows_static_and_dynamic() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist.add(chat_id=CHAT_ID, username="booksbot", added_by=ADMIN_ID, reason="поиск")
    await whitelist_list(message=make_message(bot), settings=settings, whitelist=whitelist)
    text = answer(bot)
    assert "Статический" in text and "Динамический" in text
    assert "booksbot" in text and "поиск" in text


async def test_empty_list_is_rendered() -> None:
    settings, whitelist, bot = await make_env()
    await whitelist_list(message=make_message(bot), settings=settings, whitelist=whitelist)
    assert "пусто" in answer(bot)


# ------------------------------- /spamcheck --------------------------------


async def test_spamcheck_explains_verdict() -> None:
    settings, _, bot = await make_env()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    await spam_check(
        message=make_message(bot),
        command=command("КАЗИНО ВУЛКАН переходи bonus.top/ref/1"),
        engine=engine,
        settings=settings,
    )
    text = answer(bot)
    assert "delete" in text and "casino" in text and "link" in text


async def test_spamcheck_on_book_query_is_clean() -> None:
    settings, _, bot = await make_env()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    await spam_check(
        message=make_message(bot),
        command=command("Ищу «Майстеринг Биткоин», есть в fb2?"),
        engine=engine,
        settings=settings,
    )
    assert "allow" in answer(bot)


async def test_spamcheck_uses_profile_of_replied_author() -> None:
    settings, _, bot = await make_env()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    reply = make_reply(bot, "всем привет", name="КАЗИНО ВУЛКАН")
    await spam_check(
        message=make_message(bot, reply=reply),
        command=command(None),
        engine=engine,
        settings=settings,
    )
    text = answer(bot)
    assert "КАЗИНО ВУЛКАН" in text and "profile_keyword" in text


async def test_spamcheck_without_text_shows_usage() -> None:
    settings, _, bot = await make_env()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    await spam_check(
        message=make_message(bot), command=command(None), engine=engine, settings=settings
    )
    assert "Использование" in answer(bot)


# ---------------------------- справка и статус -----------------------------


async def test_status_shows_thresholds() -> None:
    settings, _, bot = await make_env()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    await antispam_status(message=make_message(bot), settings=settings, engine=engine)
    text = answer(bot)
    assert "6" in text and "активный" in text


async def test_dry_run_is_visible_in_status() -> None:
    settings = make_settings(dry_run=True)
    bot = FakeBot()
    engine = AntiSpamEngine(EngineConfig.from_settings(settings))
    await antispam_status(message=make_message(bot), settings=settings, engine=engine)
    assert "наблюдение" in answer(bot)


async def test_help_lists_commands() -> None:
    bot = FakeBot()
    await show_help(message=make_message(bot))
    text = answer(bot)
    assert "/whitelist_add" in text and "/spamcheck" in text


async def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            await func()
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
