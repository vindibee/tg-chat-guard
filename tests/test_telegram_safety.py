"""Тесты безопасных обёрток над Bot API.

Смысл этих функций — не падать на типовых ошибках Telegram, поэтому проверяем
именно ветки с исключениями: в обычном прогоне их никогда не видно.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.exceptions import (  # noqa: E402
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import DeleteMessage, SendMessage  # noqa: E402
from aiogram.types import Chat, Message, User  # noqa: E402

from bot.utils.telegram import (  # noqa: E402
    DEFAULT_PERMISSIONS,
    safe_ban_member,
    safe_ban_sender_chat,
    safe_delete_message,
    safe_restrict_member,
    safe_unban_member,
    safe_unrestrict_member,
)

CHAT_ID = -1001234567890
USER_ID = 999


def bad_request(text: str = "message to delete not found") -> TelegramBadRequest:
    return TelegramBadRequest(method=DeleteMessage(chat_id=CHAT_ID, message_id=1), message=text)


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendMessage(chat_id=CHAT_ID, text="x"), message="bot was kicked"
    )


def retry_after(seconds: int = 0) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=CHAT_ID, text="x"),
        message="Too Many Requests",
        retry_after=seconds,
    )


class RaisingBot:
    """Bot API, который отвечает ошибкой заданное число раз."""

    def __init__(self, error: Exception, fail_times: int | None = None) -> None:
        self.error = error
        self.fail_times = fail_times
        self.attempts = 0

    async def _boom(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.fail_times is None or self.attempts <= self.fail_times:
            raise self.error
        return True

    __call__ = _boom
    unban_chat_member = _boom
    restrict_chat_member = _boom
    ban_chat_member = _boom
    ban_chat_sender_chat = _boom
    get_chat = _boom


def make_message(bot: Any) -> Message:
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=USER_ID, is_bot=False, first_name="U"),
        text="текст",
    )
    return message.as_(bot)


# ------------------------------- удаление ----------------------------------


async def test_delete_survives_missing_message() -> None:
    assert await safe_delete_message(make_message(RaisingBot(bad_request()))) is False


async def test_delete_survives_lost_rights() -> None:
    assert await safe_delete_message(make_message(RaisingBot(forbidden()))) is False


async def test_delete_retries_once_after_rate_limit() -> None:
    bot = RaisingBot(retry_after(0), fail_times=1)
    assert await safe_delete_message(make_message(bot)) is True
    assert bot.attempts == 2, "первая попытка падает по лимиту, вторая проходит"


async def test_delete_gives_up_on_permanent_rate_limit() -> None:
    assert await safe_delete_message(make_message(RaisingBot(retry_after(0)))) is False


# ------------------------------- санкции -----------------------------------


async def test_restrict_reports_failure() -> None:
    assert await safe_restrict_member(RaisingBot(forbidden()), CHAT_ID, USER_ID, 60) is False


async def test_ban_reports_failure() -> None:
    bot = RaisingBot(bad_request("user not found"))
    assert await safe_ban_member(bot, CHAT_ID, USER_ID) is False


async def test_ban_sender_chat_reports_failure() -> None:
    assert await safe_ban_sender_chat(RaisingBot(forbidden()), CHAT_ID, -100999) is False


async def test_unban_reports_failure() -> None:
    assert await safe_unban_member(RaisingBot(bad_request()), CHAT_ID, USER_ID) is False


async def test_unrestrict_reports_failure() -> None:
    assert await safe_unrestrict_member(RaisingBot(bad_request()), CHAT_ID, USER_ID) is False


async def test_unrestrict_falls_back_to_default_permissions() -> None:
    """Права чата прочитать не вышло — ставим разумный набор по умолчанию."""

    class PartialBot(RaisingBot):
        def __init__(self) -> None:
            super().__init__(forbidden())
            self.applied: dict[str, Any] = {}

        async def get_chat(self, chat_id: int) -> Any:
            raise forbidden()

        async def restrict_chat_member(self, **kwargs: Any) -> bool:
            self.applied = kwargs
            return True

    bot = PartialBot()
    assert await safe_unrestrict_member(bot, CHAT_ID, USER_ID) is True
    assert bot.applied["permissions"] == DEFAULT_PERMISSIONS


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
