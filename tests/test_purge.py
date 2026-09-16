"""Тесты массовой очистки чата: разбор аргументов, пакеты, права."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.exceptions import (  # noqa: E402
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import DeleteMessages  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.handlers.cleanup import can_purge, parse_depth  # noqa: E402
from bot.services.purge import BATCH_LIMIT, build_id_batches, purge_recent  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_ID = 501


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "bot_token": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "clean_default_depth": 1000,
        "clean_max_depth": 5000,
        "clean_confirm_threshold": 200,
    }
    values.update(overrides)
    return Settings(**values)


def bad_request(text: str = "message can't be deleted") -> TelegramBadRequest:
    return TelegramBadRequest(
        method=DeleteMessages(chat_id=CHAT_ID, message_ids=[1]), message=text
    )


class PurgeBot(FakeBot):
    """Фейковый бот с управляемым поведением delete_messages."""

    def __init__(self, fail_from: int | None = None, error: Exception | None = None) -> None:
        super().__init__()
        self.batches: list[list[int]] = []
        self.fail_from = fail_from
        self.error = error or bad_request()

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        self.batches.append(list(message_ids))
        if self.fail_from is not None and len(self.batches) > self.fail_from:
            raise self.error
        return True


# ---------------------------- разбор аргументов ----------------------------


def test_number_argument_sets_depth() -> None:
    assert parse_depth("300", make_settings()) == 300


def test_empty_and_all_use_default_depth() -> None:
    settings = make_settings()
    assert parse_depth(None, settings) == 1000
    assert parse_depth("", settings) == 1000
    assert parse_depth("all", settings) == 1000
    assert parse_depth("ВСЕ", settings) == 1000


def test_depth_is_capped() -> None:
    """Опечатка не должна выносить всю историю."""
    assert parse_depth("999999", make_settings()) == 5000


def test_garbage_argument_is_rejected() -> None:
    for argument in ("много", "-50", "3.5"):
        try:
            parse_depth(argument, make_settings())
        except ValueError:
            continue
        raise AssertionError(f"аргумент {argument!r} должен отклоняться")


# ------------------------------- пакеты ------------------------------------


def test_ids_go_backwards_in_batches_of_100() -> None:
    batches = build_id_batches(1000, 250)
    assert [len(batch) for batch in batches] == [100, 100, 50]
    assert batches[0][0] == 999, "начинаем с предыдущего сообщения"
    assert batches[-1][-1] == 750


def test_batch_never_exceeds_api_limit() -> None:
    batches = build_id_batches(5000, 1000, batch_size=500)
    assert all(len(batch) <= BATCH_LIMIT for batch in batches)


def test_excluded_ids_are_not_touched() -> None:
    batches = build_id_batches(100, 50, exclude={99, 98})
    flat = [message_id for batch in batches for message_id in batch]
    assert 99 not in flat and 98 not in flat
    assert 97 in flat


def test_ids_never_go_below_one() -> None:
    batches = build_id_batches(5, 100)
    flat = [message_id for batch in batches for message_id in batch]
    assert flat == [4, 3, 2, 1]


# ------------------------------- удаление ----------------------------------


async def test_all_batches_are_sent() -> None:
    bot = PurgeBot()
    stats = await purge_recent(bot, CHAT_ID, 1000, 250, pause=0)
    assert len(bot.batches) == 3
    assert stats.attempted == 250
    assert stats.failed_batches == 0


async def test_failed_batch_does_not_stop_the_loop() -> None:
    """Уже удалённые сообщения не должны прерывать уборку."""
    bot = PurgeBot(fail_from=1)
    bot.fail_from = None  # падает только второй пакет
    calls = {"n": 0}

    async def flaky(chat_id: int, message_ids: list[int]) -> bool:
        calls["n"] += 1
        bot.batches.append(list(message_ids))
        if calls["n"] == 2:
            raise bad_request()
        return True

    bot.delete_messages = flaky  # type: ignore[assignment]
    stats = await purge_recent(bot, CHAT_ID, 1000, 300, pause=0)
    assert len(bot.batches) == 3, "цикл продолжился после ошибки"
    assert stats.attempted == 200
    assert stats.skipped == 100
    assert stats.failed_batches == 1


async def test_three_failures_in_a_row_stop_the_purge() -> None:
    """Дальше 48 часов идти бессмысленно — останавливаемся."""
    bot = PurgeBot(fail_from=0)
    stats = await purge_recent(bot, CHAT_ID, 10_000, 1000, pause=0)
    assert len(bot.batches) == 3
    assert stats.stopped_early is True
    assert stats.skipped == 1000


async def test_missing_rights_stop_immediately() -> None:
    bot = PurgeBot(
        fail_from=0,
        error=TelegramForbiddenError(
            method=DeleteMessages(chat_id=CHAT_ID, message_ids=[1]),
            message="not enough rights",
        ),
    )
    stats = await purge_recent(bot, CHAT_ID, 1000, 500, pause=0)
    assert stats.forbidden is True
    assert len(bot.batches) == 1
    assert "Удаление сообщений" in stats.summary


async def test_flood_wait_is_retried_once() -> None:
    attempts = {"n": 0}

    class FloodBot(PurgeBot):
        async def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TelegramRetryAfter(
                    method=DeleteMessages(chat_id=chat_id, message_ids=message_ids),
                    message="Too Many Requests",
                    retry_after=0,
                )
            self.batches.append(list(message_ids))
            return True

    bot = FloodBot()
    stats = await purge_recent(bot, CHAT_ID, 200, 50, pause=0)
    assert attempts["n"] == 2, "после паузы пакет повторяется"
    assert stats.attempted == 50


async def test_pause_between_batches_is_applied() -> None:
    import time

    bot = PurgeBot()
    started = time.perf_counter()
    await purge_recent(bot, CHAT_ID, 1000, 300, pause=0.05)
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.1, "между тремя пакетами должно быть две паузы"


# --------------------------- проверка прав ---------------------------------


async def test_creator_may_purge() -> None:
    bot = FakeBot()
    bot.members[ADMIN_ID] = SimpleNamespace(status="creator")
    assert await can_purge(bot, CHAT_ID, ADMIN_ID) is True


async def test_admin_needs_delete_right() -> None:
    bot = FakeBot()
    bot.members[ADMIN_ID] = SimpleNamespace(status="administrator", can_delete_messages=True)
    assert await can_purge(bot, CHAT_ID, ADMIN_ID) is True

    bot.members[ADMIN_ID] = SimpleNamespace(status="administrator", can_delete_messages=False)
    assert await can_purge(bot, CHAT_ID, ADMIN_ID) is False


async def test_regular_member_may_not_purge() -> None:
    bot = FakeBot()
    bot.members[999] = SimpleNamespace(status="member")
    assert await can_purge(bot, CHAT_ID, 999) is False


async def test_api_error_denies_purge() -> None:
    class BrokenBot(FakeBot):
        async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
            raise bad_request("chat not found")

    assert await can_purge(BrokenBot(), CHAT_ID, ADMIN_ID) is False


async def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            result = func()
            if asyncio.iscoroutine(result):
                await result
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
