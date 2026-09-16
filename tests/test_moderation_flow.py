"""Сквозной тест модерации: сообщение → санкция → журнал → карточка админам."""

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
from bot.handlers.moderation import moderate_message  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig  # noqa: E402
from bot.services.deleted_registry import DeletedMessageRegistry  # noqa: E402
from bot.services.duplicate_detector import DuplicateDetector  # noqa: E402
from bot.services.event_service import EventService  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
USER_ID = 999

SPAM = "КАЗИНО ВУЛКАН — бонус 5000р, регистрируйся по ссылке bonus-vulkan.top/ref/12"
CLEAN = "Посоветуйте книги по трейдингу, желательно fb2"


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        admin_log_chat_id=ADMIN_CHAT_ID,
        notify_chat=False,
        reputation_providers="cas_export",
        **overrides,
    )


def make_message(text: str, bot: FakeBot) -> Message:
    message = Message(
        message_id=42,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Спамер", username="spammer"),
        text=text,
    )
    return message.as_(bot)


async def run_flow(
    text: str, *, blocklisted: bool = False, **overrides: Any
) -> tuple[FakeBot, EventService, ActivityTracker]:
    settings = make_settings(**overrides)
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    events = EventService(database)
    activity = ActivityTracker(settings, FakeRedis())
    reputation = ReputationService(settings, None, FakeSession())
    if blocklisted:
        reputation.load_cas_snapshot([USER_ID])
    bot = FakeBot()
    await moderate_message(
        message=make_message(text, bot),
        bot=bot,
        settings=settings,
        engine=AntiSpamEngine(EngineConfig.from_settings(settings)),
        activity=activity,
        events=events,
        reputation=reputation,
        duplicates=DuplicateDetector(settings, FakeRedis()),
        deleted_registry=DeletedMessageRegistry(),
        message_count=0,
    )
    return bot, events, activity


async def test_spam_is_deleted_logged_and_reported() -> None:
    bot, events, _ = await run_flow(SPAM)
    assert bot.called("DeleteMessage"), "сообщение должно быть удалено"

    card = bot.payload("send_message")
    assert card["chat_id"] == ADMIN_CHAT_ID, "карточка уходит в админ-чат"
    assert "Книжный клуб" in card["text"] and "casino" in card["text"]
    assert card["reply_markup"] is not None, "у карточки должны быть кнопки отката"

    stored = await events.recent_false_positives(CHAT_ID)
    assert stored == [], "событие ещё не разобрано"
    event = await events.get(1)
    assert event is not None and event.action.startswith("delete") and event.deleted is True


async def test_clean_message_counts_toward_approval() -> None:
    bot, events, activity = await run_flow(CLEAN)
    assert not bot.called("DeleteMessage")
    assert not bot.called("send_message"), "чистое сообщение не тревожит админов"
    assert await activity.get_count(CHAT_ID, USER_ID) == 1
    assert await events.get(1) is None


async def test_dry_run_reports_but_does_not_delete() -> None:
    bot, events, _ = await run_flow(SPAM, dry_run=True)
    assert not bot.called("DeleteMessage"), "в режиме наблюдения ничего не удаляем"
    card = bot.payload("send_message")
    assert "Наблюдение" in card["text"]
    event = await events.get(1)
    assert event is not None and event.deleted is False


async def test_spam_resets_approval_progress() -> None:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    activity = ActivityTracker(settings, FakeRedis())
    for _ in range(4):
        await activity.register_clean_message(CHAT_ID, USER_ID)
    bot = FakeBot()
    await moderate_message(
        message=make_message(SPAM, bot),
        bot=bot,
        settings=settings,
        engine=AntiSpamEngine(EngineConfig.from_settings(settings)),
        activity=activity,
        events=EventService(database),
        reputation=ReputationService(settings, None, FakeSession()),
        duplicates=DuplicateDetector(settings, FakeRedis()),
        deleted_registry=DeletedMessageRegistry(),
        message_count=4,
    )
    assert await activity.get_count(CHAT_ID, USER_ID) == 0


async def test_blocklisted_author_tips_the_scale() -> None:
    """Стоп-слово без ссылки обычно проходит, но у автора из блок-листа — нет."""
    clean_bot, clean_events, _ = await run_flow("казино бонус сегодня")
    assert not clean_bot.called("DeleteMessage")

    bot, events, _ = await run_flow("казино бонус сегодня", blocklisted=True)
    assert bot.called("DeleteMessage")
    event = await events.get(1)
    assert event is not None and "external_ban" in event.signals


async def test_blocklisted_author_may_still_talk() -> None:
    """Попадание в блок-лист — сигнал, а не приговор: обычная фраза проходит."""
    bot, events, _ = await run_flow(
        "Всем привет, ищу книгу про Древний Рим", blocklisted=True
    )
    assert not bot.called("DeleteMessage")
    assert await events.get(1) is None


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
