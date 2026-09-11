"""Тесты middleware доверия: кто и почему не доходит до модерации."""

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
FLIBUSTA_ID = 555001


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        flibusta_bot_id=FLIBUSTA_ID,
        flibusta_username="flibustafreebookbot",
        static_whitelist_ids="111",
        super_admin_ids="777",
        trust_chat_admins=True,
        **overrides,
    )


def make_message(
    user_id: int, username: str | None = None, *, chat_type: str = "supergroup"
) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID if chat_type != "private" else user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Tester", username=username),
        text="заработок на крипте, переходи по ссылке http://scam.top",
    )


async def make_middleware(
    redis: FakeRedis | None = None, **overrides: Any
) -> tuple[WhitelistMiddleware, WhitelistService]:
    settings = make_settings(**overrides)
    database = create_database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    whitelist = WhitelistService(settings, database, None, local_ttl=0.0)
    activity = ActivityTracker(settings, redis)
    middleware = WhitelistMiddleware(settings, whitelist, ChatAdminCache(), activity)
    return middleware, whitelist


class HandlerSpy:
    """Считает вызовы защищаемого хендлера."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Message, data: dict[str, Any]) -> str:
        self.calls += 1
        return "handled"


async def run_middleware(user_id: int, username: str | None = None, **kwargs: Any):
    middleware, whitelist = await make_middleware(
        kwargs.get("redis"), **kwargs.get("settings", {})
    )
    if "prepare" in kwargs:
        await kwargs["prepare"](whitelist)
    spy = HandlerSpy()
    data: dict[str, Any] = {}
    message = make_message(user_id, username, chat_type=kwargs.get("chat_type", "supergroup"))
    await middleware(spy, message, data)
    return spy, data


async def test_flibusta_bot_is_never_moderated() -> None:
    spy, data = await run_middleware(FLIBUSTA_ID, "flibustafreebookbot")
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.FLIBUSTA


async def test_static_whitelist_user_is_skipped() -> None:
    spy, data = await run_middleware(111)
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.STATIC_CONFIG


async def test_private_chat_is_not_moderated() -> None:
    spy, data = await run_middleware(999, chat_type="private")
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.PRIVATE_CHAT


async def test_regular_user_reaches_moderation() -> None:
    spy, data = await run_middleware(999, "spammer")
    assert spy.calls == 1
    assert data["trust_reason"] is None


async def test_dynamically_whitelisted_user_is_skipped() -> None:
    async def prepare(whitelist: WhitelistService) -> None:
        await whitelist.add(chat_id=CHAT_ID, user_id=999, added_by=777)

    spy, data = await run_middleware(999, "trusted_user", prepare=prepare)
    assert spy.calls == 0
    assert data["trust_reason"] is TrustReason.DYNAMIC_WHITELIST


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
