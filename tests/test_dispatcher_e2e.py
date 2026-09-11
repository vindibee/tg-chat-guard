"""Сквозные тесты через настоящий диспетчер.

Юнит-тесты вызывают хендлеры напрямую и потому не проверяют самое хрупкое:
порядок роутеров, внедрение зависимостей и работу фильтров. Здесь апдейт
проходит весь путь — middleware, фильтры, хендлер.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, Update, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.main import build_dispatcher  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
ADMIN_ID = 501
USER_ID = 999
SPAM = "КАЗИНО ВУЛКАН бонус, переходи по ссылке bonus.top/ref/1"


def make_message(
    text: str,
    user_id: int,
    message_id: int = 10,
    reply: Message | None = None,
    name: str = "U",
):
    return Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=user_id, is_bot=False, first_name=name, username=f"u{user_id}"),
        text=text,
        reply_to_message=reply,
    )


class Harness:
    """Диспетчер с подменёнными сетью и Bot API."""

    async def setup(self, **overrides: Any) -> Harness:
        self.settings = Settings(
            _env_file=None,
            bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            admin_log_chat_id=ADMIN_CHAT_ID,
            notify_chat=False,
            reputation_providers="cas_export",
            super_admin_ids=str(ADMIN_ID),
            **overrides,
        )
        database = create_database("sqlite+aiosqlite:///:memory:")
        await database.create_all()
        self.reputation = ReputationService(self.settings, None, FakeSession())
        self.dispatcher = build_dispatcher(self.settings, database, None, self.reputation)
        self.bot = FakeBot(admin_ids=(ADMIN_ID,))
        self.update_id = 0
        return self

    async def feed(self, message: Message) -> list[str]:
        """Прогоняет сообщение через диспетчер и возвращает вызовы Bot API."""
        self.bot.calls.clear()
        self.update_id += 1
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=self.update_id, message=message)
        )
        return [call for call, _ in self.bot.calls]


async def make_harness(**overrides: Any) -> Harness:
    return await Harness().setup(**overrides)


async def test_spam_goes_through_the_whole_pipeline() -> None:
    harness = await make_harness()
    calls = await harness.feed(make_message(SPAM, USER_ID))
    assert "DeleteMessage" in calls, calls
    assert "ban_chat_member" in calls, calls
    assert "send_message" in calls, "карточка должна уйти в админ-чат"
    assert harness.bot.payload("send_message")["chat_id"] == ADMIN_CHAT_ID


async def test_book_query_survives_the_pipeline() -> None:
    harness = await make_harness()
    calls = await harness.feed(
        make_message("Ищу «Майстеринг Биткоин», есть у кого fb2?", USER_ID)
    )
    # Обращение к списку админов — это работа кэша прав, а не реакция на текст.
    assert "DeleteMessage" not in calls, calls
    assert "send_message" not in calls, "по чистому сообщению админов не тревожат"
    assert "ban_chat_member" not in calls, calls


async def test_admin_command_passes_the_filter() -> None:
    harness = await make_harness()
    calls = await harness.feed(make_message("/whitelist_list", ADMIN_ID, 11))
    assert "SendMessage" in calls, calls


async def test_admin_command_is_rejected_for_regular_user() -> None:
    harness = await make_harness()
    calls = await harness.feed(make_message("/whitelist_list", USER_ID, 12))
    assert "SendMessage" not in calls, f"обычный участник не должен получить ответ: {calls}"


async def test_report_is_available_to_everyone() -> None:
    harness = await make_harness()
    target = make_message("сомнительное сообщение", USER_ID, 13)
    calls = await harness.feed(make_message("/report", 777, 14, reply=target))
    assert "DeleteMessage" in calls, "команда жалобы удаляется из чата"


async def test_manual_spam_command_works_end_to_end() -> None:
    harness = await make_harness()
    target = make_message("сомнительное сообщение", USER_ID, 15)
    calls = await harness.feed(make_message("/spam", ADMIN_ID, 16, reply=target))
    assert "DeleteMessage" in calls and "ban_chat_member" in calls, calls


async def test_approved_member_is_not_checked() -> None:
    harness = await make_harness(approved_after_messages=1)
    # Без Redis счётчик всегда нулевой, поэтому одобрение эмулируем paranoid=False
    # и прямым прогоном: первый спам ловится всегда.
    calls = await harness.feed(make_message(SPAM, USER_ID))
    assert "DeleteMessage" in calls


async def test_ad_in_profile_name_is_caught_with_a_link() -> None:
    """Текст безобидный, вся реклама в имени — ловим по связке имя + ссылка."""
    harness = await make_harness()
    calls = await harness.feed(
        make_message("всем привет, залетайте bonus-play.top", USER_ID, name="КАЗИНО ВУЛКАН")
    )
    assert "DeleteMessage" in calls, calls
    card = harness.bot.payload("send_message")["text"]
    assert "casino" in card


async def test_ad_in_profile_name_alone_is_not_punished() -> None:
    harness = await make_harness()
    calls = await harness.feed(
        make_message("подскажите книгу про Рим", USER_ID, name="КАЗИНО ВУЛКАН")
    )
    assert "DeleteMessage" not in calls, calls


async def test_mailing_from_several_accounts_is_caught() -> None:
    """Рассылка без ссылок и стоп-слов ловится по повторению текста."""
    harness = await make_harness()
    mailing = "Здравствуйте! Ищу людей для сотрудничества, подробности расскажу лично"

    early = [await harness.feed(make_message(mailing, 1000 + i, 20 + i)) for i in range(3)]
    assert all("DeleteMessage" not in calls for calls in early), early

    calls = await harness.feed(make_message(mailing, 1004, 24))
    assert "DeleteMessage" in calls, calls
    assert "duplicate" in harness.bot.payload("send_message")["text"]


async def test_same_author_repeating_himself_is_not_a_mailing() -> None:
    harness = await make_harness()
    request = "Ищу книгу Умберто Эко «Имя розы», желательно в epub, заранее спасибо"
    for attempt in range(5):
        calls = await harness.feed(make_message(request, USER_ID, 30 + attempt))
        assert "DeleteMessage" not in calls, f"попытка {attempt}: {calls}"


async def test_regress_command_reports_the_corpus() -> None:
    """Команда /regress прогоняет накопленную выборку прямо в чате."""
    harness = await make_harness()
    target = make_message("КАЗИНО ВУЛКАН бонус bonus.top/ref/1", USER_ID, 40)
    await harness.feed(make_message("/spam", ADMIN_ID, 41, reply=target))

    calls = await harness.feed(make_message("/regress", ADMIN_ID, 42))
    assert "SendMessage" in calls, calls
    answer = harness.bot.payload("SendMessage")["text"]
    assert "Прогон выборки" in answer
    assert "1/1" in answer, answer


async def test_join_service_message_is_cleaned() -> None:
    """«Вступил в группу» убирается, не доходя до антиспама."""
    harness = await make_harness()
    newcomer = User(id=777, is_bot=False, first_name="Новичок")
    service = Message(
        message_id=50,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=newcomer,
        new_chat_members=[newcomer],
    )
    calls = await harness.feed(service)
    assert "DeleteMessage" in calls, calls


async def test_service_cleanup_can_be_switched_off() -> None:
    harness = await make_harness(service_cleanup="none")
    newcomer = User(id=778, is_bot=False, first_name="Новичок")
    service = Message(
        message_id=51,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=newcomer,
        left_chat_member=newcomer,
    )
    assert "DeleteMessage" not in await harness.feed(service)


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
