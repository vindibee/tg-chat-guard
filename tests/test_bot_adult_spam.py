"""Завуалированный 18+ спам через ботов и leetspeak.

Повод: в чат пришло «super h0t vide0s avaible on this bot ... - @llkaebot», и
фильтр его пропустил. Причин было две: leet-вариант текста строился с
кириллическими заменами («h0t» -> «hоt») и обратно в латиницу не переводился,
а упоминание бота весило как упоминание обычного человека.

Обратная сторона тоже закреплена: в книжном чате ботов советуют постоянно,
а «Секс в большом городе» и «горячие новинки» — это не реклама.
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
    SignalKind,
)
from bot.services.stopwords import Category  # noqa: E402
from bot.services.text_cleaner import TextCleaner  # noqa: E402

SETTINGS = Settings(_env_file=None, bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))

#: Точный текст, который бот пропустил в живом чате.
REPORTED = "super h0t vide0s avaible on this bot ... - @llkaebot"

SPAM: tuple[str, ...] = (
    REPORTED,
    "h0t g1rls waiting for you @girls_hub_bot",
    "𝐬𝐮𝐩𝐞𝐫 𝐡𝟎𝐭 𝐯𝐢𝐝𝐞𝟎𝐬 @llkaebot",
    "n5fw c0ntent, 0n1y here @spicy_b0t",
    "Private videos available in our bot t.me/leaks_vip_bot",
    "Горячие видео уже ждут, переходи в бота @hot_video_bot",
    "Г0ряч1е в1де0 18+ @priv_bot",
    "Слив фото девушек, всё в нашем боте @sliv_bot",
)

LEGIT: tuple[str, ...] = (
    "Ищу книгу, её выдаёт @flibustafreebookbot",
    "Попробуйте @librebook_bot, там бывает epub",
    "Пиши в бота название книги, он найдёт",
    "Кто читал «Секс в большом городе»? Стоит того?",
    "Горячие новинки фантастики этого месяца — делитесь списками",
    "Hot Water Music by Bukowski, anyone has the pdf?",
    "The Naked Sun, Азимов — лучший детектив про роботов",
    "Прочитал 1984 и Fahrenheit 451, что дальше?",
    "Нужен covid19 справочник в pdf, b2b маркетинг тоже подойдёт",
    "Try the bot, it has almost every book in epub",
)


def check(text: str, **kwargs: Any) -> Any:
    safe = frozenset({SETTINGS.flibusta_username})
    return ENGINE.evaluate(MessageContext(text=text, known_safe_usernames=safe, **kwargs))


def test_reported_message_is_caught() -> None:
    verdict = check(REPORTED)
    assert verdict.is_spam, verdict.explain()
    assert Category.ADULT in verdict.categories
    assert any(signal.kind is SignalKind.BOT_MENTION for signal in verdict.signals)


def test_disguised_adult_spam_is_caught() -> None:
    for text in SPAM:
        verdict = check(text)
        assert verdict.is_spam, f"пропущен спам: {text!r} -> {verdict.explain()}"


def test_book_talk_survives() -> None:
    for text in LEGIT:
        verdict = check(text)
        assert verdict.action is Action.ALLOW, (
            f"ложное срабатывание: {text!r} -> {verdict.explain()}"
        )


def test_leet_is_normalized_to_latin() -> None:
    normalized = TextCleaner.normalize("super h0t vide0s 0n1y")
    assert any("hot videos" in variant for variant in normalized.variants)
    assert any("only" in variant for variant in normalized.variants)
    assert normalized.obfuscated


def test_digits_at_word_edges_are_not_obfuscation() -> None:
    for text in ("mp3 и fb2", "4k видео", "x10 book", "1984"):
        assert not TextCleaner.normalize(text).obfuscated, text


def test_bot_mention_replaces_plain_mention() -> None:
    verdict = check("@ivan_petrov и @promo_bot")
    kinds = [signal.kind for signal in verdict.signals]
    assert SignalKind.BOT_MENTION in kinds
    assert SignalKind.MENTION not in kinds


def test_bot_mention_alone_is_not_enough() -> None:
    """Инвариант модуля: один голый фактор без темы и призыва не наказывается."""
    verdict = check("@some_random_bot")
    assert verdict.action is Action.ALLOW, verdict.explain()


def test_masked_bot_suffix_is_detected() -> None:
    for username in ("@spicy_b0t", "@hot_8ot", "@promoBOT"):
        verdict = check(username)
        assert any(s.kind is SignalKind.BOT_MENTION for s in verdict.signals), username


def test_whitelisted_bot_is_not_a_factor() -> None:
    verdict = check("горячие видео? нет, только книги в @flibustafreebookbot")
    assert not any(s.kind is SignalKind.BOT_MENTION for s in verdict.signals)
    assert verdict.action is Action.ALLOW, verdict.explain()


def test_bot_mention_weight_comes_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        bot_mention_weight=9.0,
    )
    config = EngineConfig.from_settings(settings)
    assert config.weight(SignalKind.BOT_MENTION) == 9.0
