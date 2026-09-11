"""Тесты детектора рассылок: один текст с нескольких аккаунтов."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.services.antispam_engine import (  # noqa: E402
    Action,
    AntiSpamEngine,
    EngineConfig,
    MessageContext,
    SignalKind,
)
from bot.services.duplicate_detector import DuplicateDetector  # noqa: E402
from bot.services.text_cleaner import TextCleaner  # noqa: E402
from tests.fake_redis import FakeRedis  # noqa: E402

CHAT_ID = -1001234567890
MAILING = "Здравствуйте! Ищу людей для сотрудничества, подробности расскажу лично"
BOOK = "Ищу книгу Умберто Эко «Имя розы», желательно в epub, заранее спасибо"


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        duplicate_threshold=3,
        duplicate_min_length=40,
        **overrides,
    )


# ------------------------------ отпечаток ----------------------------------


def test_fingerprint_ignores_cosmetic_changes() -> None:
    base = TextCleaner.fingerprint("Привет!!! Ищу людей для работы")
    assert base == TextCleaner.fingerprint("привет, ищу  людеи для работы")
    assert base == TextCleaner.fingerprint("ПРИВЕТ 🙂 Ищу людей, для работы...")


def test_fingerprint_differs_for_different_texts() -> None:
    assert TextCleaner.fingerprint("Ищу книгу про Рим") != TextCleaner.fingerprint(
        "Ищу людей для работы"
    )


# ------------------------------ счётчик ------------------------------------


async def test_counts_distinct_authors() -> None:
    detector = DuplicateDetector(make_settings(), FakeRedis())
    for expected, user_id in enumerate((1, 2, 3), start=1):
        assert await detector.register(CHAT_ID, user_id, MAILING) == expected


async def test_same_author_counts_once() -> None:
    """Человек, поднявший свой запрос второй раз, не должен пострадать."""
    detector = DuplicateDetector(make_settings(), FakeRedis())
    for _ in range(5):
        assert await detector.register(CHAT_ID, 42, BOOK) == 1


async def test_short_messages_are_ignored() -> None:
    detector = DuplicateDetector(make_settings(), FakeRedis())
    for user_id in (1, 2, 3, 4):
        assert await detector.register(CHAT_ID, user_id, "спасибо!") == 0


async def test_chats_are_independent() -> None:
    detector = DuplicateDetector(make_settings(), FakeRedis())
    await detector.register(CHAT_ID, 1, MAILING)
    assert await detector.register(-100999, 2, MAILING) == 1


async def test_works_without_redis() -> None:
    detector = DuplicateDetector(make_settings(), None)
    assert await detector.register(CHAT_ID, 1, MAILING) == 1
    assert await detector.register(CHAT_ID, 2, MAILING) == 2
    assert detector.is_duplicate(await detector.register(CHAT_ID, 3, MAILING))


async def test_disabled_detector_counts_nothing() -> None:
    detector = DuplicateDetector(make_settings(duplicate_enabled=False), FakeRedis())
    assert await detector.register(CHAT_ID, 1, MAILING) == 0


async def test_falls_back_to_memory_when_redis_is_down() -> None:
    detector = DuplicateDetector(make_settings(), FakeRedis(fail=True))
    assert await detector.register(CHAT_ID, 1, MAILING) == 1
    assert await detector.register(CHAT_ID, 2, MAILING) == 2


# ------------------------------- движок ------------------------------------

ENGINE = AntiSpamEngine(EngineConfig.from_settings(make_settings()))


def check(text: str, authors: int) -> Any:
    return ENGINE.evaluate(MessageContext(text=text, duplicate_authors=authors))


def test_mailing_is_caught_without_any_stopword() -> None:
    """Главный сценарий: ни ссылок, ни стоп-слов — только повторение."""
    assert check(MAILING, 1).action is Action.ALLOW
    assert check(MAILING, 3).action is Action.ALLOW, "три автора — только повод присмотреться"
    assert check(MAILING, 4).is_spam
    assert check(MAILING, 5).action is Action.DELETE_AND_MUTE


def test_weight_grows_with_number_of_authors() -> None:
    assert check(MAILING, 3).score < check(MAILING, 4).score < check(MAILING, 5).score


def test_weight_is_capped() -> None:
    assert check(MAILING, 9).score == check(MAILING, 20).score


def test_book_intent_does_not_excuse_a_mailing() -> None:
    """Слово «ищу» в рассылке не превращает её в книжный запрос."""
    verdict = check(BOOK, 5)
    assert verdict.is_spam, verdict.explain()
    assert SignalKind.DUPLICATE in {signal.kind for signal in verdict.signals}


def test_single_author_is_never_a_duplicate() -> None:
    assert check(BOOK, 1).action is Action.ALLOW
    assert check(BOOK, 2).action is Action.ALLOW


def test_duplicate_combines_with_other_signals() -> None:
    """Три автора сами по себе слабы, но вместе со ссылкой дают удаление."""
    verdict = check(f"{MAILING} подробнее тут promo-site.top", 3)
    assert verdict.is_spam, verdict.explain()


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
