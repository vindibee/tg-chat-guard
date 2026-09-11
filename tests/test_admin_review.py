"""Тесты карточки модерации: журнал событий и откат ложного срабатывания."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import CallbackQuery, Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.admin_review import (  # noqa: E402
    ReviewCallback,
    build_keyboard,
    confirm_spam,
    render_card,
    undo_false_positive,
    whitelist_from_card,
)
from bot.services.admin_cache import ChatAdminCache  # noqa: E402
from bot.services.event_service import EventService  # noqa: E402
from bot.services.whitelist_service import WhitelistService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402

CHAT_ID = -1001234567890
SPAMMER_ID = 999
ADMIN_ID = 501
OUTSIDER_ID = 777


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        admin_log_chat_id=-1009999999999,
        super_admin_ids="",
        **overrides,
    )


def make_message(text: str = "казино вулкан, переходи по ссылке scam.top") -> Message:
    return Message(
        message_id=42,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=User(id=SPAMMER_ID, is_bot=False, first_name="Спамер", username="spammer"),
        text=text,
    )


def make_callback(action: str, event_id: int, user_id: int) -> CallbackQuery:
    return CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="Админ"),
        chat_instance="ci-1",
        data=ReviewCallback(action=action, event_id=event_id).pack(),
        message=None,
    )


async def make_env(action: str = "delete_and_ban") -> tuple[Any, ...]:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    events = EventService(database)
    whitelist = WhitelistService(settings, database, None, local_ttl=0.0)
    event = await events.record(
        chat_id=CHAT_ID,
        user_id=SPAMMER_ID,
        username="spammer",
        message_id=42,
        action=action,
        reason="spam_detected",
        score=13.5,
        categories="casino",
        signals="keyword,link",
        excerpt="казино вулкан, переходи по ссылке scam.top",
        deleted=True,
    )
    bot = FakeBot(admin_ids=(ADMIN_ID,))
    return settings, events, whitelist, event, bot


# ------------------------------- журнал -----------------------------------


async def test_event_is_recorded_with_id() -> None:
    _, events, _, event, _ = await make_env()
    assert event is not None and event.id > 0
    stored = await events.get(event.id)
    assert stored is not None
    assert stored.action == "delete_and_ban"
    assert not stored.is_reviewed


async def test_mark_reviewed_records_admin() -> None:
    _, events, _, event, _ = await make_env()
    updated = await events.mark_reviewed(event.id, admin_id=ADMIN_ID, false_positive=True)
    assert updated is not None
    assert updated.false_positive is True
    assert updated.reviewed_by == ADMIN_ID
    assert updated.reviewed_at is not None
    assert [e.id for e in await events.recent_false_positives(CHAT_ID)] == [event.id]


# ------------------------------- карточка ---------------------------------


def test_card_contains_key_facts() -> None:
    class _Stub:
        id = 7
        action = "delete_and_mute"
        score = 9.5
        reason = "spam_detected"
        categories = "casino"
        signals = "keyword,link"
        excerpt = "казино вулкан"

    card = render_card(_Stub(), make_message(), dry_run=False)
    assert "9.5" in card and "Книжный клуб" in card and "casino" in card
    assert "казино вулкан" in card and "event #7" in card
    assert f'tg://user?id={SPAMMER_ID}' in card


def test_dry_run_card_is_marked() -> None:
    class _Stub:
        id = 8
        action = "delete"
        score = 6.0
        reason = "spam_detected"
        categories = ""
        signals = ""
        excerpt = "текст"

    assert "Наблюдение" in render_card(_Stub(), make_message(), dry_run=True)


def test_callback_data_roundtrip() -> None:
    packed = ReviewCallback(action="fp", event_id=123).pack()
    parsed = ReviewCallback.unpack(packed)
    assert parsed.action == "fp" and parsed.event_id == 123
    buttons = [b for row in build_keyboard(123).inline_keyboard for b in row]
    assert len(buttons) == 3
    assert all(b.callback_data.startswith("rv:") for b in buttons)
    closed = [b for row in build_keyboard(123, with_undo=False).inline_keyboard for b in row]
    assert len(closed) == 1


# ------------------------------- откат ------------------------------------


async def test_undo_unbans_restores_and_marks_false_positive() -> None:
    settings, events, whitelist, event, bot = await make_env("delete_and_ban")
    callback = make_callback("fp", event.id, ADMIN_ID).as_(bot)

    await undo_false_positive(
        callback=callback,
        callback_data=ReviewCallback(action="fp", event_id=event.id),
        bot=bot,
        settings=settings,
        events=events,
        admin_cache=ChatAdminCache(),
    )

    assert bot.called("unban_chat_member")
    assert bot.payload("unban_chat_member")["user_id"] == SPAMMER_ID
    restored = bot.payload("send_message")
    assert restored["chat_id"] == CHAT_ID and "восстановлено" in restored["text"]

    stored = await events.get(event.id)
    assert stored is not None and stored.false_positive is True
    assert stored.reviewed_by == ADMIN_ID


async def test_undo_of_mute_returns_permissions() -> None:
    settings, events, _, event, bot = await make_env("delete_and_mute")
    callback = make_callback("fp", event.id, ADMIN_ID).as_(bot)

    await undo_false_positive(
        callback=callback,
        callback_data=ReviewCallback(action="fp", event_id=event.id),
        bot=bot,
        settings=settings,
        events=events,
        admin_cache=ChatAdminCache(),
    )
    assert bot.called("restrict_chat_member")
    assert not bot.called("unban_chat_member")


async def test_outsider_cannot_undo() -> None:
    settings, events, _, event, bot = await make_env()
    callback = make_callback("fp", event.id, OUTSIDER_ID).as_(bot)

    await undo_false_positive(
        callback=callback,
        callback_data=ReviewCallback(action="fp", event_id=event.id),
        bot=bot,
        settings=settings,
        events=events,
        admin_cache=ChatAdminCache(),
    )
    assert not bot.called("unban_chat_member")
    stored = await events.get(event.id)
    assert stored is not None and stored.false_positive is False


async def test_confirm_marks_reviewed_without_undo() -> None:
    settings, events, _, event, bot = await make_env()
    callback = make_callback("ok", event.id, ADMIN_ID).as_(bot)

    await confirm_spam(
        callback=callback,
        callback_data=ReviewCallback(action="ok", event_id=event.id),
        bot=bot,
        settings=settings,
        events=events,
        admin_cache=ChatAdminCache(),
    )
    stored = await events.get(event.id)
    assert stored is not None
    assert stored.reviewed_by == ADMIN_ID and stored.false_positive is False
    assert not bot.called("unban_chat_member")


async def test_whitelist_button_adds_author() -> None:
    settings, events, whitelist, event, bot = await make_env()
    callback = make_callback("wl", event.id, ADMIN_ID).as_(bot)

    await whitelist_from_card(
        callback=callback,
        callback_data=ReviewCallback(action="wl", event_id=event.id),
        bot=bot,
        settings=settings,
        events=events,
        whitelist=whitelist,
        admin_cache=ChatAdminCache(),
    )
    assert await whitelist.is_trusted(CHAT_ID, SPAMMER_ID, "spammer")


async def test_missing_event_is_handled() -> None:
    settings, events, _, _, bot = await make_env()
    callback = make_callback("fp", 10_000, ADMIN_ID).as_(bot)
    await undo_false_positive(
        callback=callback,
        callback_data=ReviewCallback(action="fp", event_id=10_000),
        bot=bot,
        settings=settings,
        events=events,
        admin_cache=ChatAdminCache(),
    )
    assert not bot.called("unban_chat_member")


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
