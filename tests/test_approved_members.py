"""Тесты области проверки: одобренные участники не доходят до модерации."""

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
from bot.services.whitelist_service import WhitelistService  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
USER_ID = 999


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        approved_after_messages=7,
        new_member_messages=5,
        **overrides,
    )


def make_message() -> Message:
    return Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Reader", username="reader"),
        text="заработок на крипте, переходи по ссылке http://scam.top",
    )


class HandlerSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Message, data: dict[str, Any]) -> str:
        self.calls += 1
        return "handled"


async def run_gate(redis: FakeRedis, **overrides: Any) -> tuple[HandlerSpy, dict[str, Any]]:
    settings = make_settings(**overrides)
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    middleware = WhitelistMiddleware(
        settings,
        WhitelistService(settings, database, None, local_ttl=0.0),
        ChatAdminCache(),
        ActivityTracker(settings, redis),
    )
    spy = HandlerSpy()
    data: dict[str, Any] = {}
    await middleware(spy, make_message(), data)
    return spy, data


# --------------------------- счётчик --------------------------------------


async def test_only_clean_messages_count() -> None:
    tracker = ActivityTracker(make_settings(), FakeRedis())
    assert await tracker.get_count(CHAT_ID, USER_ID) == 0
    for expected in (1, 2, 3):
        assert await tracker.register_clean_message(CHAT_ID, USER_ID) == expected
    assert await tracker.get_count(CHAT_ID, USER_ID) == 3


async def test_spam_resets_progress() -> None:
    tracker = ActivityTracker(make_settings(), FakeRedis())
    for _ in range(6):
        await tracker.register_clean_message(CHAT_ID, USER_ID)
    await tracker.reset(CHAT_ID, USER_ID)
    assert await tracker.get_count(CHAT_ID, USER_ID) == 0


def test_thresholds() -> None:
    tracker = ActivityTracker(make_settings())
    assert tracker.is_new_member(0) and tracker.is_new_member(4)
    assert not tracker.is_new_member(5)
    assert not tracker.is_approved(6)
    assert tracker.is_approved(7)


def test_paranoid_mode_disables_approval() -> None:
    tracker = ActivityTracker(make_settings(paranoid_mode=True))
    assert not tracker.is_approved(100)


async def test_without_redis_nobody_is_approved() -> None:
    """Безопасная деградация: без кэша проверяются все, а не никто."""
    tracker = ActivityTracker(make_settings(), None)
    assert await tracker.get_count(CHAT_ID, USER_ID) == 0
    assert not tracker.is_approved(await tracker.get_count(CHAT_ID, USER_ID))


# --------------------------- шлюз доверия ----------------------------------


async def test_approved_member_skips_moderation() -> None:
    redis = FakeRedis()
    tracker = ActivityTracker(make_settings(), redis)
    for _ in range(7):
        await tracker.register_clean_message(CHAT_ID, USER_ID)
    spy, data = await run_gate(redis)
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.APPROVED_MEMBER


async def test_newcomer_is_checked_and_count_is_passed() -> None:
    redis = FakeRedis()
    tracker = ActivityTracker(make_settings(), redis)
    for _ in range(3):
        await tracker.register_clean_message(CHAT_ID, USER_ID)
    spy, data = await run_gate(redis)
    assert spy.calls == 1
    assert data["trust_reason"] is None
    assert data["message_count"] == 3


async def test_paranoid_mode_checks_everyone() -> None:
    redis = FakeRedis()
    tracker = ActivityTracker(make_settings(), redis)
    for _ in range(50):
        await tracker.register_clean_message(CHAT_ID, USER_ID)
    spy, data = await run_gate(redis, paranoid_mode=True)
    assert spy.calls == 1
    assert data["trust_reason"] is None


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
