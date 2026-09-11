"""Тесты проверки новичков на входе в чат."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import (  # noqa: E402
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    User,
)

from bot.config import Settings  # noqa: E402
from bot.db.session import create_database  # noqa: E402
from bot.handlers.join_guard import check_new_member  # noqa: E402
from bot.services.reputation_service import ReputationService  # noqa: E402
from bot.services.whitelist_service import WhitelistService  # noqa: E402
from tests.fake_bot import FakeBot  # noqa: E402
from tests.fake_http import FakeSession  # noqa: E402

CHAT_ID = -1001234567890
ADMIN_CHAT_ID = -1009999999999
SPAMMER_ID = 6660001
CLEAN_ID = 555


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "bot_token": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "admin_log_chat_id": ADMIN_CHAT_ID,
        "reputation_providers": "cas_export",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_join(user_id: int, *, is_bot: bool = False, username: str | None = None):
    user = User(id=user_id, is_bot=is_bot, first_name="Новичок", username=username)
    return ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type="supergroup", title="Книжный клуб"),
        from_user=user,
        date=datetime.now(tz=UTC),
        old_chat_member=ChatMemberLeft(user=user, status="left"),
        new_chat_member=ChatMemberMember(user=user, status="member"),
    )


async def run_guard(
    user_id: int, *, blocklisted: bool = True, trusted: bool = False, **overrides: Any
) -> FakeBot:
    settings = make_settings(**overrides)
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    whitelist = WhitelistService(settings, database, None, local_ttl=0.0)
    if trusted:
        await whitelist.add(chat_id=CHAT_ID, user_id=user_id, added_by=1)

    reputation = ReputationService(settings, None, FakeSession())
    if blocklisted:
        reputation.load_cas_snapshot([user_id])

    bot = FakeBot()
    await check_new_member(
        event=make_join(user_id),
        bot=bot,
        settings=settings,
        reputation=reputation,
        whitelist=whitelist,
    )
    return bot


async def test_blocklisted_newcomer_alerts_admins() -> None:
    bot = await run_guard(SPAMMER_ID)
    alert = bot.payload("send_message")
    assert alert["chat_id"] == ADMIN_CHAT_ID
    assert "блок-листа" in alert["text"]
    assert str(SPAMMER_ID) in alert["text"]
    assert not bot.called("ban_chat_member"), "по умолчанию бот только предупреждает"


async def test_clean_newcomer_is_ignored() -> None:
    bot = await run_guard(CLEAN_ID, blocklisted=False)
    assert bot.calls == []


async def test_autoban_bans_and_reports() -> None:
    bot = await run_guard(SPAMMER_ID, reputation_autoban=True)
    assert bot.called("ban_chat_member")
    assert "забанен автоматически" in bot.payload("send_message")["text"]


async def test_whitelisted_newcomer_is_not_checked() -> None:
    bot = await run_guard(SPAMMER_ID, trusted=True)
    assert bot.calls == [], "доверенного участника не проверяем даже по блок-листу"


async def test_bots_are_skipped() -> None:
    settings = make_settings()
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    reputation = ReputationService(settings, None, FakeSession())
    reputation.load_cas_snapshot([SPAMMER_ID])
    bot = FakeBot()
    await check_new_member(
        event=make_join(SPAMMER_ID, is_bot=True),
        bot=bot,
        settings=settings,
        reputation=reputation,
        whitelist=WhitelistService(settings, database, None, local_ttl=0.0),
    )
    assert bot.calls == []


async def test_disabled_reputation_skips_the_check() -> None:
    bot = await run_guard(SPAMMER_ID, reputation_enabled=False)
    assert bot.calls == []


async def test_without_admin_chat_nothing_is_sent() -> None:
    bot = await run_guard(SPAMMER_ID, admin_log_chat_id=None)
    assert not bot.called("send_message")


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
