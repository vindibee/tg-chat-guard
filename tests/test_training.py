"""Тесты ручной разметки (/spam, /ham) и жалоб участников (/report)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.admin_training import mark_ham, mark_spam, report_message  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.admin_cache import ChatAdminCache  # noqa: E402
from bot.services.event_service import EventService  # noqa: E402
from bot.services.report_service import ReportService  # noqa: E402
from bot.services.sample_service import HAM, SPAM, SampleService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
ADMIN_ID = 356404555
SPAMMER_ID = 999
READER_ID = 555
SPAM_TEXT = "Заходи в казино, бонус 5000 http://scam.top"


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        admin_log_chat_id=ADMIN_CHAT_ID,
        super_admin_ids=str(ADMIN_ID),
        report_threshold=2,
        **overrides,
    )


def make_message(
    text: str, user_id: int, bot: FakeBot, reply_to: Message | None = None
) -> Message:
    message = Message(
        message_id=100 if reply_to is None else 101,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=user_id, is_bot=False, first_name="Кто-то", username="someone"),
        text=text,
        reply_to_message=reply_to,
    )
    return message.as_(bot)


class Env:
    """Готовое окружение хендлеров."""

    def __init__(self) -> None:
        self.settings = make_settings()
        self.bot = FakeBot(admin_ids=(ADMIN_ID,))
        self.redis = FakeRedis()

    async def setup(self) -> Env:
        database = create_database("sqlite+aiosqlite:///:memory:")
        await database.create_all()
        self.events = EventService(database)
        self.samples = SampleService(database)
        self.activity = ActivityTracker(self.settings, self.redis)
        self.reports = ReportService(self.settings, self.redis)
        self.admin_cache = ChatAdminCache()
        return self


async def make_env() -> Env:
    return await Env().setup()


# --------------------------------- /spam -----------------------------------


async def test_spam_deletes_bans_and_stores_sample() -> None:
    env = await make_env()
    target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)
    command = make_message("/spam", ADMIN_ID, env.bot, reply_to=target)

    await mark_spam(
        message=command,
        bot=env.bot,
        settings=env.settings,
        events=env.events,
        samples=env.samples,
        activity=env.activity,
        admin_cache=env.admin_cache,
    )

    assert env.bot.called("DeleteMessage")
    assert env.bot.called("ban_chat_member")
    assert (await env.samples.counts()).get(SPAM) == 1
    event = await env.events.get(1)
    assert event is not None and event.reason == "manual_spam"


async def test_spam_does_not_touch_admins() -> None:
    env = await make_env()
    target = make_message("обычное сообщение", ADMIN_ID, env.bot)
    command = make_message("/spam", ADMIN_ID, env.bot, reply_to=target)

    await mark_spam(
        message=command,
        bot=env.bot,
        settings=env.settings,
        events=env.events,
        samples=env.samples,
        activity=env.activity,
        admin_cache=env.admin_cache,
    )
    assert not env.bot.called("ban_chat_member")
    assert await env.events.get(1) is None


async def test_duplicate_sample_is_not_stored_twice() -> None:
    env = await make_env()
    for _ in range(2):
        target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)
        command = make_message("/spam", ADMIN_ID, env.bot, reply_to=target)
        await mark_spam(
            message=command,
            bot=env.bot,
            settings=env.settings,
            events=env.events,
            samples=env.samples,
            activity=env.activity,
            admin_cache=env.admin_cache,
        )
    assert (await env.samples.counts()).get(SPAM) == 1


# ---------------------------------- /ham -----------------------------------


async def test_ham_undoes_ban_and_marks_event() -> None:
    env = await make_env()
    target = make_message("Ищу книгу про криптографию", READER_ID, env.bot)
    event = await env.events.record(
        chat_id=CHAT_ID,
        user_id=READER_ID,
        username="reader",
        message_id=target.message_id,
        action="delete_and_ban",
        reason="spam_detected",
        score=13.0,
        categories="crypto_scam",
        signals="keyword,link",
        excerpt="Ищу книгу про криптографию",
        deleted=True,
    )
    command = make_message("/ham", ADMIN_ID, env.bot, reply_to=target)

    await mark_ham(
        message=command,
        bot=env.bot,
        settings=env.settings,
        events=env.events,
        samples=env.samples,
    )

    assert env.bot.called("unban_chat_member")
    stored = await env.events.get(event.id)
    assert stored is not None and stored.false_positive is True
    assert (await env.samples.counts()).get(HAM) == 1


async def test_ham_without_event_just_stores_sample() -> None:
    env = await make_env()
    target = make_message("Просто хороший текст", READER_ID, env.bot)
    command = make_message("/ham", ADMIN_ID, env.bot, reply_to=target)

    await mark_ham(
        message=command,
        bot=env.bot,
        settings=env.settings,
        events=env.events,
        samples=env.samples,
    )
    assert not env.bot.called("unban_chat_member")
    assert (await env.samples.counts()).get(HAM) == 1


# --------------------------------- /report ---------------------------------


async def _send_report(env: Env, reporter_id: int, target: Message) -> None:
    command = make_message("/report", reporter_id, env.bot, reply_to=target)
    await report_message(
        message=command,
        bot=env.bot,
        settings=env.settings,
        reports=env.reports,
    )


async def test_admins_are_called_after_threshold() -> None:
    env = await make_env()
    target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)

    await _send_report(env, READER_ID, target)
    assert not env.bot.called("send_message"), "одной жалобы мало"

    await _send_report(env, READER_ID + 1, target)
    alert = env.bot.payload("send_message")
    assert alert["chat_id"] == ADMIN_CHAT_ID
    assert "Жалобы участников: 2" in alert["text"]
    assert "t.me/c/1234567890/100" in alert["text"]


async def test_repeated_report_from_same_user_does_not_count() -> None:
    env = await make_env()
    target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)
    await _send_report(env, READER_ID, target)
    await _send_report(env, READER_ID, target)
    assert not env.bot.called("send_message")


async def test_self_report_is_ignored() -> None:
    env = await make_env()
    target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)
    await _send_report(env, SPAMMER_ID, target)
    assert await env.reports.register(CHAT_ID, target.message_id, READER_ID) == 1


async def test_report_command_is_removed_from_chat() -> None:
    env = await make_env()
    target = make_message(SPAM_TEXT, SPAMMER_ID, env.bot)
    await _send_report(env, READER_ID, target)
    assert env.bot.called("DeleteMessage")


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
