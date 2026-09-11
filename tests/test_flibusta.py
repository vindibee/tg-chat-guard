"""Тесты иммунитета книжного бота и уборки его «мусорных» ответов."""

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
from bot.handlers.flibusta_filter import (  # noqa: E402
    build_router,
    clean_flibusta_reply,
    detect_junk,
)
from bot.handlers.moderation import moderate_message  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig  # noqa: E402
from bot.services.deleted_registry import DeletedMessageRegistry  # noqa: E402
from bot.services.duplicate_detector import DuplicateDetector  # noqa: E402
from bot.services.event_service import EventService  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402

CHAT_ID = -1001234567890
FLIBUSTA_ID = 8156231123
SPAMMER_ID = 999
SPAM = "КАЗИНО ВУЛКАН бонус, переходи по ссылке bonus.top/ref/1"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "bot_token": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "flibusta_bot_id": FLIBUSTA_ID,
        "notify_chat": False,
        "reputation_providers": "cas_export",
    }
    values.update(overrides)
    return Settings(**values)


def flibusta_message(
    bot: FakeBot, text: str, reply_to: Message | None = None, message_id: int = 20
) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=FLIBUSTA_ID, is_bot=True, first_name="Флибуста"),
        text=text,
        reply_to_message=reply_to,
    )
    return message.as_(bot)


def user_message(
    bot: FakeBot, text: str, user_id: int = SPAMMER_ID, message_id: int = 10
) -> Message:
    message = Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=user_id, is_bot=False, first_name="Читатель"),
        text=text,
    )
    return message.as_(bot)


async def run_moderation(
    bot: FakeBot, message: Message, registry: DeletedMessageRegistry, settings: Settings
) -> EventService:
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    events = EventService(database)
    await moderate_message(
        message=message,
        bot=bot,
        settings=settings,
        engine=AntiSpamEngine(EngineConfig.from_settings(settings)),
        activity=ActivityTracker(settings, None),
        events=events,
        reputation=ReputationService(settings, None, FakeSession()),
        duplicates=DuplicateDetector(settings, None),
        deleted_registry=registry,
        message_count=0,
    )
    return events


# ------------------------------- иммунитет ---------------------------------


async def test_flibusta_is_never_moderated() -> None:
    """Даже спамный текст от книжного бота не влечёт санкций."""
    bot = FakeBot()
    settings = make_settings()
    events = await run_moderation(
        bot, flibusta_message(bot, SPAM), DeletedMessageRegistry(), settings
    )
    assert bot.calls == [], "книжному боту нельзя ни удалений, ни санкций"
    assert await events.get(1) is None, "и спам-баллов тоже"


# ----------------------------- мусорные ответы -----------------------------


def test_not_found_reply_is_junk() -> None:
    bot = FakeBot()
    registry = DeletedMessageRegistry()
    for text in (
        "По запросу «Толстой» не найдено книг",
        "Ничего не найдено, уточните запрос",
        "К сожалению, книг не найдено",
    ):
        assert detect_junk(flibusta_message(bot, text), registry) == "not_found", text


def test_cross_mark_reply_is_junk() -> None:
    bot = FakeBot()
    message = flibusta_message(bot, "❌ По запросу ничего нет, попробуйте иначе")
    assert detect_junk(message, DeletedMessageRegistry()) == "failed_search"


def test_reply_to_deleted_message_is_junk() -> None:
    """Запрос удалён антиспамом — ответ на него висит в пустоте."""
    bot = FakeBot()
    registry = DeletedMessageRegistry()
    original = user_message(bot, SPAM, message_id=10)
    registry.remember(CHAT_ID, original.message_id)
    reply = flibusta_message(bot, "Вот что нашлось: Война и мир", reply_to=original)
    assert detect_junk(reply, registry) == "orphan_reply"


def test_useful_reply_is_kept() -> None:
    bot = FakeBot()
    original = user_message(bot, "Ищу Войну и мир", message_id=11)
    reply = flibusta_message(bot, "Нашлось 3 книги: Война и мир…", reply_to=original)
    assert detect_junk(reply, DeletedMessageRegistry()) is None


async def test_junk_reply_is_deleted() -> None:
    bot = FakeBot()
    await clean_flibusta_reply(
        message=flibusta_message(bot, "По запросу не найдено книг"),
        settings=make_settings(),
        deleted_registry=DeletedMessageRegistry(),
    )
    assert bot.called("DeleteMessage")


async def test_useful_reply_is_not_deleted() -> None:
    bot = FakeBot()
    await clean_flibusta_reply(
        message=flibusta_message(bot, "Нашлось 3 книги: Война и мир…"),
        settings=make_settings(),
        deleted_registry=DeletedMessageRegistry(),
    )
    assert not bot.called("DeleteMessage")


async def test_dry_run_keeps_junk() -> None:
    bot = FakeBot()
    await clean_flibusta_reply(
        message=flibusta_message(bot, "не найдено книг"),
        settings=make_settings(dry_run=True),
        deleted_registry=DeletedMessageRegistry(),
    )
    assert not bot.called("DeleteMessage")


async def test_cleanup_can_be_disabled() -> None:
    bot = FakeBot()
    await clean_flibusta_reply(
        message=flibusta_message(bot, "не найдено книг"),
        settings=make_settings(flibusta_cleanup=False),
        deleted_registry=DeletedMessageRegistry(),
    )
    assert not bot.called("DeleteMessage")


def test_router_is_empty_without_bot_id() -> None:
    assert build_router(make_settings(flibusta_bot_id=None)).message.handlers == []
    assert len(build_router(make_settings()).message.handlers) == 1


# --------------------------- связка с модерацией ---------------------------


async def test_moderation_records_deletion_for_the_filter() -> None:
    """Сквозной сценарий: спам удалён -> ответ книжного бота на него убран."""
    settings = make_settings()
    registry = DeletedMessageRegistry()
    bot = FakeBot()
    spam = user_message(bot, SPAM, message_id=10)

    await run_moderation(bot, spam, registry, settings)
    assert bot.called("DeleteMessage")
    assert registry.was_deleted(CHAT_ID, 10), "модерация запомнила удаление"

    bot.calls.clear()
    await clean_flibusta_reply(
        message=flibusta_message(bot, "Нашлось 5 книг", reply_to=spam),
        settings=settings,
        deleted_registry=registry,
    )
    assert bot.called("DeleteMessage"), "ответ в пустоту тоже убран"


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
