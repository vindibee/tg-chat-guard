"""Тесты проверки профиля автора: реклама в имени и username."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Chat, Message, User  # noqa: E402

from bot.config import Settings  # noqa: E402
from bot.handlers.moderation import _author_identity, build_context  # noqa: E402
from bot.services.activity_tracker import ActivityTracker  # noqa: E402
from bot.services.antispam_engine import (  # noqa: E402
    Action,
    AntiSpamEngine,
    EngineConfig,
    MessageContext,
    SignalKind,
)
from bot.services.stopwords import Category  # noqa: E402

CHAT_ID = -1001234567890
SETTINGS = Settings(
    bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    safe_domains="flibusta.is",
)
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))
TRACKER = ActivityTracker(SETTINGS, None)


def check(text: str, name: str = "", username: str = "") -> Any:
    return ENGINE.evaluate(
        MessageContext(text=text, author_name=name, author_username=username)
    )


def kinds(verdict: Any) -> set[SignalKind]:
    return {signal.kind for signal in verdict.signals}


# --- имя как рекламный баннер ----------------------------------------------


def test_ad_in_name_alone_is_not_punished() -> None:
    """Одно лишь имя не наказывается — как и одно лишь стоп-слово в тексте."""
    verdict = check("привет всем", name="КАЗИНО ВУЛКАН")
    assert verdict.action is Action.ALLOW
    assert SignalKind.PROFILE_KEYWORD in kinds(verdict)
    assert Category.CASINO in verdict.categories


def test_ad_in_name_plus_link_is_spam() -> None:
    verdict = check("смотрите тут bonus-play.top", name="КАЗИНО ВУЛКАН")
    assert verdict.is_spam, verdict.explain()


def test_ad_in_username_is_detected() -> None:
    verdict = check("вот ссылка scam.xyz", name="Аня", username="casino_promo_bot")
    assert verdict.is_spam, verdict.explain()


def test_invite_link_in_name_is_a_factor() -> None:
    verdict = check("книги тут, пиши в лс", name="Заработок t.me/+abcdef")
    assert verdict.is_spam, verdict.explain()
    assert SignalKind.PROFILE_LINK in kinds(verdict)


def test_adult_name_with_call_to_action_is_spam() -> None:
    verdict = check("переходи в бот @hot_channel", name="Интим досуг 18+")
    assert verdict.is_spam, verdict.explain()


def test_profile_plus_bare_mention_stays_below_threshold() -> None:
    """Осознанная граница: упоминания собеседника мало для наказания.

    Имя-баннер весит 3.5, упоминание — 2.0; вместе 5.5 при пороге удаления 6.0.
    Так участник с сомнительным ником, ответивший «@вася привет», не пострадает.
    """
    verdict = check("@vasya привет", name="Интим досуг 18+")
    assert verdict.action is Action.ALLOW
    assert verdict.score == 5.5, verdict.explain()


# --- анти-FP ----------------------------------------------------------------


def test_neutral_finance_nickname_is_ignored() -> None:
    """«Криптоведьмак» и «Биткоин Иваныч» — это читатели, а не спамеры."""
    for name in ("Криптоведьмак", "Биткоин Иваныч", "Трейдер Пётр"):
        verdict = check("Ищу книгу про Древний Рим", name=name)
        assert verdict.action is Action.ALLOW, name
        assert SignalKind.PROFILE_KEYWORD not in kinds(verdict), name


def test_ordinary_name_adds_nothing() -> None:
    verdict = check("Посоветуйте книги по трейдингу", name="Анна Каренина")
    assert verdict.action is Action.ALLOW
    assert verdict.score == 0.5, verdict.explain()


def test_safe_domain_in_name_is_not_a_link() -> None:
    verdict = check("книги тут", name="Читаю flibusta.is")
    assert SignalKind.PROFILE_LINK not in kinds(verdict)
    assert verdict.action is Action.ALLOW


def test_empty_profile_is_handled() -> None:
    verdict = check("обычное сообщение")
    assert verdict.action is Action.ALLOW
    assert not verdict.signals


# --- сборка контекста из объекта aiogram ------------------------------------


def make_message(name: str, username: str | None, text: str = "привет") -> Message:
    first, _, last = name.partition(" ")
    return Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        from_user=User(
            id=999, is_bot=False, first_name=first, last_name=last or None, username=username
        ),
        text=text,
    )


def test_identity_is_taken_from_user() -> None:
    name, username = _author_identity(make_message("КАЗИНО ВУЛКАН", "Promo_Bot"))
    assert name == "КАЗИНО ВУЛКАН"
    assert username == "promo_bot"


def test_identity_falls_back_to_sender_chat() -> None:
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=CHAT_ID, type="supergroup"),
        sender_chat=Chat(id=-100999, type="channel", title="Казино Новости", username="CasinoNews"),
        text="привет",
    )
    name, username = _author_identity(message)
    assert name == "Казино Новости" and username == "casinonews"


def test_context_carries_profile() -> None:
    context = build_context(
        make_message("КАЗИНО ВУЛКАН", "promo"), SETTINGS, TRACKER, message_count=0
    )
    assert context.author_name == "КАЗИНО ВУЛКАН"
    assert context.author_username == "promo"
    assert ENGINE.evaluate(context).categories == frozenset({Category.CASINO})


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
