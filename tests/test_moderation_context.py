"""Тесты сборки контекста из объекта aiogram и сквозной проверки движка."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, MessageEntity, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.handlers.moderation import build_context  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.antispam_engine import Action, AntiSpamEngine, EngineConfig  # noqa: E402

CHAT_ID = -1001234567890
SETTINGS = Settings(
    _env_file=None,
    bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    flibusta_username="flibustafreebookbot",
    safe_domains="flibusta.is",
)
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))
TRACKER = ActivityTracker(SETTINGS, None)


def make_message(
    text: str, entities: list[MessageEntity] | None = None, reply_to: Message | None = None
) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(id=999, is_bot=False, first_name="Tester", username="tester"),
        text=text,
        entities=entities,
        reply_to_message=reply_to,
    )


async def test_hidden_link_entity_reaches_engine() -> None:
    text = "Отличная книга про крипту — забирай тут"
    message = make_message(
        text,
        entities=[
            MessageEntity(type="text_link", offset=0, length=8, url="https://scam-invest.top/ref/7")
        ],
    )
    context = build_context(message, SETTINGS, TRACKER, message_count=0)
    assert context.entity_urls == ("https://scam-invest.top/ref/7",)
    verdict = ENGINE.evaluate(context)
    assert verdict.action is not Action.ALLOW, verdict.explain()


async def test_book_search_with_safe_domain_is_allowed() -> None:
    text = "Ищу Майстеринг Биткоин, на flibusta.is не нашёл"
    message = make_message(text)
    context = build_context(message, SETTINGS, TRACKER, message_count=0)
    verdict = ENGINE.evaluate(context)
    assert verdict.action is Action.ALLOW, verdict.explain()


async def test_reply_mention_is_not_a_spam_factor() -> None:
    replied = make_message("Что почитать по финансам?")
    message = make_message("@tester посмотри книги по инвестициям", reply_to=replied)
    context = build_context(message, SETTINGS, TRACKER, message_count=0)
    assert "tester" in context.known_safe_usernames
    verdict = ENGINE.evaluate(context)
    assert verdict.action is Action.ALLOW, verdict.explain()


async def test_mention_entity_is_extracted() -> None:
    text = "казино тут @scam_channel заходи"
    message = make_message(
        text, entities=[MessageEntity(type="mention", offset=11, length=13)]
    )
    context = build_context(message, SETTINGS, TRACKER, message_count=0)
    assert "scam_channel" in context.mentions
    verdict = ENGINE.evaluate(context)
    assert verdict.is_spam, verdict.explain()


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
