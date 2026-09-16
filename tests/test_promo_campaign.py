"""Рекламная кампания одного бота: разные тексты, один автор, один бот.

Повод: 16.09.2026 участник «Himanshu Sharma» с 12:47 до 19:45 каждые ~40 минут
постил в чат @llkaebot, чередуя три-четыре текста. Бот пропускал всё: каждое
сообщение по отдельности набирало 4.5 балла, а пропущенная реклама ещё и
засчитывалась как «чистое» сообщение — через семь штук автор получил бы
иммунитет и больше не проверялся бы вовсе.

Здесь лента прогоняется через настоящий обработчик с общими счётчиками, как в
живом чате, включая шаг WhitelistMiddleware (одобренные не проверяются).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.moderation import moderate_message  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.antispam_engine import (  # noqa: E402
    Action,
    AntiSpamEngine,
    EngineConfig,
    MessageContext,
    SignalKind,
)
from bot.services.deleted_registry import DeletedMessageRegistry  # noqa: E402
from bot.services.duplicate_detector import DuplicateDetector  # noqa: E402
from bot.services.event_service import EventService  # noqa: E402
from bot.services.promo_tracker import PromoTracker  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
USER_ID = 777

#: Лента из чата — дословно, в исходном порядке.
CAMPAIGN: tuple[str, ...] = (
    "videos bot= @llkaebot",
    "bhai log sub yha scam kr rhe h ye bot me real videos h  @llkaebot",
    "videos bot= @llkaebot",
    "check BEST vide0s and meet girls on this bot = @llkaebot",
    "bhai log sub yha scam kr rhe h ye bot me real videos h  @llkaebot",
    "videos bot= @llkaebot",
    "bhai log sub yha scam kr rhe h ye bot me real videos h  @llkaebot",
    "check BEST vide0s and meet girls on this bot = @llkaebot",
    "desi vide0s and girlss bot =  @llkaebot",
    "check BEST vide0s and meet girls on this bot = @llkaebot",
    "bhai log sub yha scam kr rhe h ye bot me real videos h  @llkaebot",
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        notify_chat=False,
        reputation_providers="cas_export",
        **overrides,
    )


class Chatroom:
    """Один чат с общим Redis: как в проде, между сообщениями ничего не сбрасывается."""

    def __init__(self, **overrides: Any) -> None:
        self.settings = make_settings(**overrides)
        self.redis = FakeRedis()
        self.activity = ActivityTracker(self.settings, self.redis)
        self.duplicates = DuplicateDetector(self.settings, self.redis)
        self.promo = PromoTracker(self.settings, self.redis)
        self.engine = AntiSpamEngine(EngineConfig.from_settings(self.settings))
        self.reputation = ReputationService(self.settings, None, FakeSession())
        self.database = create_database("sqlite+aiosqlite:///:memory:")
        self.events = EventService(self.database)
        self.message_id = 0

    async def close(self) -> None:
        await self.database.dispose()

    async def post(self, text: str, user_id: int = USER_ID) -> FakeBot | None:
        """Отправляет сообщение. None — участник одобрен и не проверялся."""
        await self.database.create_all()
        count = await self.activity.get_count(CHAT_ID, user_id)
        if self.activity.is_approved(count):
            return None
        self.message_id += 1
        bot = FakeBot()
        message = Message(
            message_id=self.message_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
            from_user=User(id=user_id, is_bot=False, first_name="Himanshu Sharma"),
            text=text,
        ).as_(bot)
        await moderate_message(
            message=message,
            bot=bot,
            settings=self.settings,
            engine=self.engine,
            activity=self.activity,
            events=self.events,
            reputation=self.reputation,
            duplicates=self.duplicates,
            promo=self.promo,
            deleted_registry=DeletedMessageRegistry(),
            message_count=count,
        )
        return bot


@pytest.fixture
async def make_room() -> AsyncIterator[Callable[..., Chatroom]]:
    """Фабрика чатов, закрывающая базы после теста (иначе ResourceWarning)."""
    rooms: list[Chatroom] = []

    def factory(**overrides: Any) -> Chatroom:
        room = Chatroom(**overrides)
        rooms.append(room)
        return room

    yield factory
    for room in rooms:
        await room.close()


async def test_whole_campaign_is_removed_and_escalates(
    make_room: Callable[..., Chatroom],
) -> None:
    room = make_room()
    sanctions: list[str] = []
    for text in CAMPAIGN:
        bot = await room.post(text)
        assert bot is not None, f"спамер получил иммунитет перед: {text!r}"
        assert bot.called("DeleteMessage"), f"пропущено: {text!r}"
        if bot.called("ban_chat_member"):
            sanctions.append("ban")
        elif bot.called("restrict_chat_member"):
            sanctions.append("mute")
        else:
            sanctions.append("delete")
    assert sanctions[1] in {"mute", "ban"}, f"повтор должен наказываться строже: {sanctions}"
    assert "ban" in sanctions, f"кампания должна дойти до бана: {sanctions}"


async def test_first_post_of_a_newcomer_is_deleted(make_room: Callable[..., Chatroom]) -> None:
    """Самый короткий вариант без единого стоп-слова — «videos bot= @llkaebot»."""
    room = make_room()
    bot = await room.post("videos bot= @llkaebot")
    assert bot is not None and bot.called("DeleteMessage")


async def test_missed_promo_does_not_earn_immunity(make_room: Callable[..., Chatroom]) -> None:
    """Пропущенная реклама не должна приближать автора к статусу «одобренного»."""
    room = make_room(newcomer_promo_weight=0.0, promo_repeat_weight=0.0)
    text = "Попробуйте @librebook_bot, там бывает epub"
    for _ in range(10):
        bot = await room.post(text, user_id=555)
        assert bot is not None, "автор рекламы стал одобренным"
        assert not bot.called("DeleteMessage")
    assert await room.activity.get_count(CHAT_ID, 555) == 0


async def test_clean_talk_still_earns_approval(make_room: Callable[..., Chatroom]) -> None:
    room = make_room()
    for _ in range(room.settings.approved_after_messages):
        await room.post("Дочитал «Сто лет одиночества», под впечатлением", user_id=556)
    assert await room.post("что почитать дальше?", user_id=556) is None


async def test_approved_member_may_recommend_a_bot(make_room: Callable[..., Chatroom]) -> None:
    """Старожил чата советует бота — его сообщения не проверяются вовсе."""
    room = make_room()
    for _ in range(room.settings.approved_after_messages):
        await room.post("Спасибо, нашёл в fb2", user_id=557)
    assert await room.post("Попробуйте @librebook_bot, там бывает epub", user_id=557) is None


# --------------------------------------------------------------------------- #
#                              PromoTracker                                   #
# --------------------------------------------------------------------------- #


async def test_tracker_counts_previous_mentions() -> None:
    tracker = PromoTracker(make_settings(), FakeRedis())
    assert await tracker.register(CHAT_ID, 1, ["llkaebot"]) == 0
    assert await tracker.register(CHAT_ID, 1, ["llkaebot"]) == 1
    assert await tracker.register(CHAT_ID, 1, ["llkaebot", "other_bot"]) == 2


async def test_tracker_is_per_author() -> None:
    tracker = PromoTracker(make_settings(), FakeRedis())
    await tracker.register(CHAT_ID, 1, ["librebook_bot"])
    assert await tracker.register(CHAT_ID, 2, ["librebook_bot"]) == 0


async def test_tracker_falls_back_to_memory() -> None:
    for redis in (None, FakeRedis(fail=True)):
        tracker = PromoTracker(make_settings(), redis)
        await tracker.register(CHAT_ID, 1, ["llkaebot"])
        assert await tracker.register(CHAT_ID, 1, ["llkaebot"]) == 1


async def test_tracker_ignores_empty_input() -> None:
    tracker = PromoTracker(make_settings(), FakeRedis())
    assert await tracker.register(CHAT_ID, 1, []) == 0
    assert await tracker.register(CHAT_ID, 0, ["llkaebot"]) == 0


def test_promo_targets_skip_trusted_bots() -> None:
    engine = AntiSpamEngine()
    context = MessageContext(
        text="@flibustafreebookbot и @llkaebot, ещё t.me/leaks_bot и @ivan",
        known_safe_usernames=frozenset({"flibustafreebookbot"}),
    )
    assert engine.promo_targets(context) == ("leaks_bot", "llkaebot")


def test_repeat_weight_is_capped() -> None:
    engine = AntiSpamEngine()
    verdict = engine.evaluate(MessageContext(text="@llkaebot", promo_repeats=10))
    repeat = [s for s in verdict.signals if s.kind is SignalKind.PROMO_REPEAT]
    assert repeat and repeat[0].weight == 12.0
    assert verdict.action is Action.DELETE_AND_BAN
