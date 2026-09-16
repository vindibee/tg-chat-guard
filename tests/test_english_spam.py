"""Англоязычный спам и защита английских книжных запросов.

Повод: в чат пришло «telegram accounts cheap store / get yours @EGRU_JI_BOT»,
и фильтр его пропустил — словарь был целиком русским. Здесь закреплены и сам
случай, и обратная сторона: в читательском чате английский звучит постоянно,
и книжные запросы страдать не должны.
"""

from __future__ import annotations

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
)
from bot.services.stopwords import Category  # noqa: E402

SETTINGS = Settings(_env_file=None, bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))

#: Точный текст, который бот пропустил в живом чате.
REPORTED = "telegram accounts cheap store\nget yours @EGRU_JI_BOT"

LEGIT: tuple[str, ...] = (
    "Looking for Mastering Bitcoin by Antonopoulos, epub please",
    "Anyone read 'Thinking, Fast and Slow'? Worth it?",
    "Is there an English edition of The Master and Margarita?",
    "Ищу книгу Financial Freedom, Грант Сабатье",
    "Посоветуйте что почитать про passive income",
    "I work from home and listen to audiobooks all day",
    "Кто-нибудь читал Rich Dad Poor Dad в оригинале?",
    "@ivan can you share the pdf of Clean Code?",
    "Нужна книга про stock market для новичков",
    "Ищу 'The Intelligent Investor', investment plan там разбирается",
    "Есть у кого Trading in the Zone?",
    "Telegram аккаунт как удалить? Вопрос не по теме, извините",
    "Кто читал про side hustle? Посоветуйте",
)

SPAM: tuple[str, ...] = (
    REPORTED,
    "Selling aged telegram accounts, dm me @seller_bot",
    "Buy followers cheap, smm panel, join now t.me/+abc",
    "Work from home and earn $500 a day, no experience needed, write me",
    "Free crypto airdrop! Connect your wallet at drop-now.top",
    "Hot girls waiting, click here sexy-chat.xyz",
    "Sports betting with deposit bonus, register now bet-win.top",
    "Виртуальные номера и смс активация, пиши в лс @nomer_shop",
)


def check(text: str) -> Any:
    return ENGINE.evaluate(MessageContext(text=text))


def test_reported_message_is_caught() -> None:
    """Тот самый пропуск из чата."""
    verdict = check(REPORTED)
    assert verdict.is_spam, verdict.explain()
    assert Category.ACCOUNT_TRADE in verdict.categories
    assert verdict.score >= 9.0


def test_english_spam_is_caught() -> None:
    for text in SPAM:
        assert check(text).is_spam, f"пропущен спам: {text!r} -> {check(text).explain()}"


def test_english_book_talk_survives() -> None:
    for text in LEGIT:
        verdict = check(text)
        assert verdict.action is Action.ALLOW, (
            f"ложное срабатывание: {text!r} -> {verdict.explain()}"
        )


def test_book_titles_are_not_job_scam() -> None:
    """«Financial Freedom» и «Passive Income» — названия книг, а не реклама."""
    for text in ("Financial Freedom", "passive income", "work from home", "side hustle"):
        verdict = check(f"Ищу книгу {text}, есть в fb2?")
        assert Category.JOB_SCAM not in verdict.categories, text
        assert verdict.action is Action.ALLOW, text


def test_account_trade_needs_a_spam_factor() -> None:
    """Инвариант сохраняется и для новой категории: одного слова мало."""
    verdict = check("telegram accounts")
    assert verdict.action is Action.ALLOW
    assert Category.ACCOUNT_TRADE in verdict.categories


def test_english_call_to_action_alone_is_not_enough() -> None:
    verdict = check("Great book! Get yours on the official site")
    assert verdict.action is Action.ALLOW, verdict.explain()


def _run() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
