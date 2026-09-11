"""Тесты уборки служебных сообщений."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.handlers.service_cleanup import build_router, clean_service_message  # noqa: E402
from bot.utils.service_kinds import (  # noqa: E402
    NEVER_DELETE,
    SERVICE_GROUPS,
    resolve_content_types,
)
from tests.fake_bot import FakeBot  # noqa: E402

CHAT_ID = -1001234567890
USER = User(id=999, is_bot=False, first_name="Новичок")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"bot_token": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
    values.update(overrides)
    return Settings(**values)


def service_message(bot: FakeBot, **payload: Any) -> Message:
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=USER,
        **payload,
    )
    return message.as_(bot)


# ------------------------------ группы -------------------------------------


def test_join_and_leave_are_resolved() -> None:
    types = resolve_content_types(["join", "leave"])
    assert "new_chat_members" in types
    assert "left_chat_member" in types


def test_all_covers_every_group() -> None:
    types = resolve_content_types(["all"])
    for name, group in SERVICE_GROUPS.items():
        assert group - NEVER_DELETE <= types, name


def test_payments_are_never_deleted() -> None:
    """Финансовые записи стирать нельзя ни при каких настройках."""
    types = resolve_content_types(["all", "other"])
    assert "successful_payment" not in types
    assert "refunded_payment" not in types


def test_migration_messages_are_kept() -> None:
    assert "migrate_to_chat_id" not in resolve_content_types(["all"])


def test_none_disables_everything() -> None:
    assert resolve_content_types(["none"]) == frozenset()
    assert resolve_content_types([]) == frozenset()


def test_unknown_group_is_skipped() -> None:
    types = resolve_content_types(["join", "чтототакое"])
    assert types == resolve_content_types(["join"])


# ------------------------------ удаление -----------------------------------


async def test_join_message_is_deleted() -> None:
    bot = FakeBot()
    message = service_message(bot, new_chat_members=[USER])
    assert message.content_type == "new_chat_members"
    await clean_service_message(message=message, settings=make_settings())
    assert bot.called("DeleteMessage")


async def test_leave_message_is_deleted() -> None:
    bot = FakeBot()
    await clean_service_message(
        message=service_message(bot, left_chat_member=USER), settings=make_settings()
    )
    assert bot.called("DeleteMessage")


async def test_title_change_is_deleted() -> None:
    bot = FakeBot()
    await clean_service_message(
        message=service_message(bot, new_chat_title="Новое название"),
        settings=make_settings(),
    )
    assert bot.called("DeleteMessage")


async def test_dry_run_keeps_service_messages() -> None:
    bot = FakeBot()
    await clean_service_message(
        message=service_message(bot, new_chat_members=[USER]),
        settings=make_settings(dry_run=True),
    )
    assert not bot.called("DeleteMessage")


async def test_failure_to_delete_is_survived() -> None:
    """Нет прав на удаление — пишем в лог и живём дальше."""

    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    class ForbiddenBot(FakeBot):
        async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
            raise TelegramForbiddenError(
                method=SendMessage(chat_id=CHAT_ID, text="x"), message="no rights"
            )

    bot = ForbiddenBot()
    await clean_service_message(
        message=service_message(bot, new_chat_members=[USER]), settings=make_settings()
    )  # не должно бросить исключение


# ------------------------------- роутер ------------------------------------


def test_router_is_empty_when_disabled() -> None:
    router = build_router(make_settings(service_cleanup="none"))
    assert router.message.handlers == []


def test_router_registers_handler_when_enabled() -> None:
    router = build_router(make_settings(service_cleanup="join,leave"))
    assert len(router.message.handlers) == 1


def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            result = func()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
