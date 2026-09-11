"""Регрессии на найденные баги. Каждый тест назван по симптому."""

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
from bot.middlewares.whitelist_middleware import TrustReason, WhitelistMiddleware  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.admin_cache import ChatAdminCache  # noqa: E402
from bot.services.report_service import ReportService  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from bot.services.text_cleaner import normalize  # noqa: E402
from bot.services.whitelist_service import WhitelistService  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402

CHAT_ID = -1001234567890
CHANNEL_ID = -1005555555555


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        report_threshold=2,
        **overrides,
    )


# --- Жалобы без Redis не доходили до порога ---------------------------------


async def test_reports_reach_threshold_without_redis() -> None:
    reports = ReportService(make_settings(), None)
    assert await reports.register(CHAT_ID, 5, reporter_id=1) == 1
    assert await reports.register(CHAT_ID, 5, reporter_id=2) == 2
    assert reports.is_enough(2)


async def test_repeated_report_ignored_without_redis() -> None:
    reports = ReportService(make_settings(), None)
    await reports.register(CHAT_ID, 5, reporter_id=1)
    assert await reports.register(CHAT_ID, 5, reporter_id=1) == 0


# --- Без Redis блок-лист опрашивался на каждое сообщение --------------------


async def test_blocklist_answer_is_cached_without_redis() -> None:
    session = FakeSession(payload={"ok": True, "banned": False})
    service = ReputationService(
        make_settings(reputation_providers="lols"), None, session
    )
    for _ in range(5):
        await service.check(777, online=True)
    assert len(session.requests) == 1


# --- Обычная русская речь считалась обходом фильтра -------------------------


def test_single_letter_words_are_not_obfuscation() -> None:
    for phrase in ("а я с ним говорил", "и я в шоке", "о ней я у них спрашивал"):
        assert not normalize(phrase).obfuscated, phrase


def test_stretched_word_is_still_caught() -> None:
    assert normalize("к а з и н о").obfuscated
    assert "казино" in normalize("к а з и н о").variants


def test_hyphenated_words_are_not_obfuscation() -> None:
    for phrase in ("какой-то роман", "из-за дождя", "что-то про финансы"):
        assert not normalize(phrase).obfuscated, phrase


# --- Сообщение от имени чужого канала считалось анонимным админом -----------


def make_channel_message(sender_chat_id: int, *, automatic: bool = False) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=None,
        sender_chat=Chat(id=sender_chat_id, type="channel", title="Канал"),
        is_automatic_forward=automatic or None,
        text="Заходи в казино, бонус по ссылке scam.top/ref/1",
    )


class HandlerSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Message, data: dict[str, Any]) -> str:
        self.calls += 1
        return "handled"


async def run_gate(message: Message) -> tuple[HandlerSpy, dict[str, Any]]:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    middleware = WhitelistMiddleware(
        settings,
        WhitelistService(settings, database, None, local_ttl=0.0),
        ChatAdminCache(),
        ActivityTracker(settings, None),
    )
    spy = HandlerSpy()
    data: dict[str, Any] = {}
    await middleware(spy, message, data)
    return spy, data


async def test_foreign_channel_is_moderated() -> None:
    spy, data = await run_gate(make_channel_message(CHANNEL_ID))
    assert spy.calls == 1, "сообщение от чужого канала должно проверяться"
    assert data["trust_reason"] is None


async def test_anonymous_admin_is_trusted() -> None:
    spy, data = await run_gate(make_channel_message(CHAT_ID))
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.ANONYMOUS_ADMIN


async def test_linked_channel_autoforward_is_trusted() -> None:
    spy, data = await run_gate(make_channel_message(CHANNEL_ID, automatic=True))
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.ANONYMOUS_ADMIN


# --- Пустой URL из битой сущности засчитывался как скрытая ссылка ----------


def test_blank_entity_url_is_not_a_signal() -> None:
    from bot.services.antispam_engine import Action, AntiSpamEngine, EngineConfig, MessageContext

    engine = AntiSpamEngine(EngineConfig.from_settings(make_settings()))
    verdict = engine.evaluate(
        MessageContext(text="Ищу книгу про Древний Рим", entity_urls=("", "   "))
    )
    assert verdict.action is Action.ALLOW
    assert verdict.score == 0.0, verdict.explain()


def test_broken_entity_offset_yields_no_url() -> None:
    from aiogram.types import MessageEntity

    from bot.utils.telegram import extract_payload

    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=1, is_bot=False, first_name="A"),
        text="короткий",
        entities=[MessageEntity(type="url", offset=100, length=50)],
    )
    _, urls, mentions = extract_payload(message)
    assert urls == () and mentions == ()


# --- В белый список попадал несуществующий кириллический username ----------


def test_cyrillic_username_is_rejected() -> None:
    from aiogram.filters import CommandObject

    from bot.handlers.admin_whitelist import parse_target
    from bot.services.whitelist_service import WhitelistError

    settings = make_settings(super_admin_ids="501")
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=501, is_bot=False, first_name="A"),
        text="/whitelist_add",
    )
    for bad in ("@пользователь", "@ab", "@" + "x" * 40):
        try:
            parse_target(
                message,
                CommandObject(prefix="/", command="whitelist_add", args=bad),
                settings,
            )
        except WhitelistError:
            continue
        raise AssertionError(f"username {bad!r} не должен приниматься")


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
