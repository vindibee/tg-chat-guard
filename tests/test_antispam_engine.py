"""Тесты движка антиспама: приоритет — отсутствие ложных срабатываний."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.services.antispam_engine import (  # noqa: E402
    Action,
    AntiSpamEngine,
    EngineConfig,
    MessageContext,
    VerdictReason,
)
from bot.services.stopwords import Category  # noqa: E402

ENGINE = AntiSpamEngine(
    EngineConfig(safe_domains=frozenset({"flibusta.is", "litres.ru", "fantlab.ru"}))
)

# --------------------------------------------------------------------------- #
#         НЕ СПАМ: книжные запросы со «спамными» на вид словами                #
# --------------------------------------------------------------------------- #

LEGIT_MESSAGES: tuple[str, ...] = (
    "Майстеринг Биткоин",
    "Ищу книгу Mastering Bitcoin, Андреас Антонопулос",
    "Криптография и безопасность сетей, Шнайер",
    "Посоветуйте книги по трейдингу и инвестициям",
    "Кто читал 'Богатый папа, бедный папа'? Стоит ли?",
    "Нужна книга про финансы для начинающих, желательно fb2",
    "Секс в большом городе — есть в аудио?",
    "Ищу учебник по экономике, автор Мэнкью",
    "Подскажите книгу как заработать на фрилансе",
    "Есть ли у кого 'Психология казино' Дорфмана?",
    "Скачать https://flibusta.is/b/123456 не получается, кто-то пробовал?",
    "Есть на litres.ru, но хочу epub",
    "@ivan_reader посоветуй что почитать про блокчейн",
    "блокчейн для чайников pdf",
    "Что почитать про пассивный доход? Не инфоцыганское",
)

SPAM_MESSAGES: tuple[str, ...] = (
    "Начни зарабатывать на крипте прямо сейчас! Переходи по ссылке https://t.me/+dA3kZq1",
    "КАЗИНО ВУЛКАН — бездепозитный бонус 5000р, регистрируйся по ссылке bonus-vulkan.top/ref/12",
    "Требуются сотрудники на удалёнку, доход от 5000 в день, пиши в лс",
    "Приватный канал сигналов на трейдинг, вход платный, @scam_signals_channel",
    "к.а.з.и.н.о онлайн, жми на ссылку casino-play.xyz",
    "18+ канал без цензуры, переходи в бот @hot_leak_bot",
    "Раздача USDT всем подписчикам! Подключи кошелёк и получи 500$ http://usdt-drop.icu?ref=99",
)


def check(text: str, **kwargs: object) -> object:
    return ENGINE.evaluate(MessageContext(text=text, **kwargs))  # type: ignore[arg-type]


def test_legit_messages_are_never_punished() -> None:
    for text in LEGIT_MESSAGES:
        verdict = check(text)
        assert (
            verdict.action is Action.ALLOW
        ), f"ложное срабатывание: {text!r} -> {verdict.explain()}"


def test_spam_messages_are_punished() -> None:
    for text in SPAM_MESSAGES:
        verdict = check(text)
        assert verdict.is_spam, f"пропущен спам: {text!r} -> {verdict.explain()}"


def test_keywords_alone_never_trigger() -> None:
    """Ключевой инвариант: стоп-слово без спам-фактора безопасно."""
    verdict = check("казино биткоин порно заработок")
    assert verdict.action is Action.ALLOW
    assert verdict.reason is VerdictReason.NO_SPAM_FACTOR


def test_hidden_link_is_detected() -> None:
    verdict = check(
        "Отличная книга про крипту, читайте",
        entity_urls=("https://scam-invest.top/ref/12",),
    )
    assert verdict.is_spam, verdict.explain()


def test_safe_domain_link_is_not_a_factor() -> None:
    verdict = check("Заработок на крипте описан тут https://flibusta.is/b/777")
    assert verdict.action is Action.ALLOW
    assert verdict.reason in {VerdictReason.SAFE_DOMAIN_ONLY, VerdictReason.NO_SPAM_FACTOR}


def test_book_intent_does_not_whitewash_aggressive_spam() -> None:
    verdict = check("Книги по крипте бесплатно! Переходи по ссылке https://t.me/+zzz")
    assert verdict.is_spam, verdict.explain()


def test_drugs_are_banned_immediately() -> None:
    verdict = check("Закладки по городу, пиши в лс @dealer_bot")
    assert verdict.action is Action.DELETE_AND_BAN
    assert Category.DRUGS in verdict.categories


def test_obfuscation_is_penalized() -> None:
    plain = check("казино играть, переходи по ссылке scam.xyz")
    obfuscated = check("к а з и н о играть, переходи по ссылке scam.xyz")
    assert obfuscated.score >= plain.score


def test_whitelisted_mention_is_not_a_factor() -> None:
    verdict = check(
        "заработок на крипте @flibustafreebookbot",
        known_safe_usernames=frozenset({"flibustafreebookbot"}),
    )
    assert verdict.action is Action.ALLOW


def _run() -> int:
    """Мини-раннер, чтобы тесты запускались и без pytest."""
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print("-" * 60)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО ТЕСТОВ: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run())
