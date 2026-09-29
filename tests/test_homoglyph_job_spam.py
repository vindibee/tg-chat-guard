"""Вербовка через буквы-двойники и скрытые ссылки `t.me/m/...`.

Повод: в чат пришло «6️⃣5️⃣ тысяч ᴇⲿᴇ∂ʜᴇвʜσ / Вσзραст σт 18 / Пσᴧучuть
uнфσρмᴀцuю» с двумя ссылками — и фильтр не нашёл в нём вообще ничего.

Дыр было три, и каждая чинится отдельно:

1. Буквы подменены двойниками из греческого, коптского и блока «малых
   заглавных». NFKD их не раскладывает, поэтому словарь видел не «ежедневно»,
   а набор неизвестных символов.
2. Цифры-«клавиши» (6️⃣) не склеивались в число.
3. `t.me/m/<хеш>` — ссылка на личные сообщения — не подпадала под регулярку
   приглашений (после слэша всего одна буква) и не давала ни одного сигнала.

Поэтому здесь проверяется не только итоговый вердикт, но и каждый слой
по отдельности: иначе починка одного из них скроет поломку соседнего.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.services.antispam_engine import (  # noqa: E402
    Action,
    AntiSpamEngine,
    EngineConfig,
    MessageContext,
    SignalKind,
    Verdict,
)
from bot.services.text_cleaner import TextCleaner  # noqa: E402

SETTINGS = Settings(_env_file=None, bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))

#: Точный текст из чата. Символы заданы кодами: в редакторе они неотличимы
#: от обычных букв, и правка «вслепую» незаметно превратила бы тест в фикцию.
JOB_SPAM = (
    "\U0001F61A\U0001F609\U0001F60C\U0001F60C\U0001F609\U0001F609\n"
    "6\uFE0F\u20E35\uFE0F\u20E3 \u0442\u044B\u0441\u044F\u0447 "
    "\u1D07\u2CBF\u1D07\u2202\u029C\u1D07\u0432\u029C\u03C3  \n"
    " \u0412\u03C3\u0437\u03C1\u03B1\u0441\u0442 \u03C3\u0442 18  \n"
    "  (https://t.me/m/vNzse78XM2Zi)\u23E9\u041F\u03C3\u1D27\u0443\u0447\u0438\u0442\u044C "
    "\u0438\u043D\u0444\u03C3\u03C1\u043C\u1D00\u0446\u0438\u044E "
    "(https://t.me/m/_WBB6R6dNTUy)"
)


def _check(text: str, **kwargs: object) -> Verdict:
    return ENGINE.evaluate(MessageContext(text=text, **kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#                        Слой 1: буквы-двойники                                #
# --------------------------------------------------------------------------- #


def test_homoglyphs_are_folded_into_plain_letters() -> None:
    """«Пσᴧучuть uнфσρмᴀцuю» должно читаться как «получить информацию»."""
    normalized = TextCleaner.normalize(JOB_SPAM)
    assert "ежедневно" in normalized.lowered, normalized.lowered
    assert "возраст от 18" in normalized.lowered, normalized.lowered
    assert "получить информацию" in normalized.lowered, normalized.lowered


def test_homoglyph_substitution_is_an_obfuscation_signal() -> None:
    """Двойник внутри слова — обход фильтра: случайно так не печатают."""
    assert TextCleaner.normalize(JOB_SPAM).obfuscated


def test_keycap_digits_become_a_number() -> None:
    """6️⃣5️⃣ — это 65, иначе «N тысяч в день» не с чем сопоставлять."""
    assert TextCleaner.normalize("6\uFE0F\u20E35\uFE0F\u20E3 тысяч").lowered == "65 тысяч"


def test_homoglyphs_give_the_same_fingerprint() -> None:
    """Рассылка, размноженная двойниками, остаётся одним и тем же текстом.

    Это чинит и детектор дубликатов: раньше каждая копия выглядела уникальной.
    """
    plain = TextCleaner.fingerprint("получить информацию")
    masked = TextCleaner.fingerprint(
        "\u041F\u03C3\u1D27\u0443\u0447\u0438\u0442\u044C "
        "\u0438\u043D\u0444\u03C3\u03C1\u043C\u1D00\u0446\u0438\u044E"
    )
    assert plain == masked


# --------------------------------------------------------------------------- #
#                     Слой 2: ссылки в личные сообщения                        #
# --------------------------------------------------------------------------- #


def test_private_message_link_is_an_invite() -> None:
    """`t.me/m/<хеш>` прячет контакт: username в такой ссылке не виден."""
    verdict = _check("Пишите мне https://t.me/m/vNzse78XM2Zi и всё расскажу")
    assert any(
        s.kind is SignalKind.TELEGRAM_INVITE for s in verdict.signals
    ), verdict.explain()


def test_single_private_link_alone_is_not_enough() -> None:
    """Одна ссылка — ещё не рассылка: порог удаления она не берёт."""
    assert _check("https://t.me/m/vNzse78XM2Zi").action is Action.ALLOW


# --------------------------------------------------------------------------- #
#                          Слой 3: итоговый вердикт                            #
# --------------------------------------------------------------------------- #


def test_job_spam_is_punished() -> None:
    verdict = _check(JOB_SPAM)
    assert verdict.action.requires_delete, verdict.explain()


def test_every_layer_fires_independently() -> None:
    """Каждый слой должен ловить сам: чтобы поломка одного была видна."""
    kinds = {signal.kind for signal in _check(JOB_SPAM).signals}
    assert SignalKind.KEYWORD in kinds, "словарь не увидел «65 тысяч ежедневно»"
    assert SignalKind.TELEGRAM_INVITE in kinds, "ссылки t.me/m не распознаны"
    assert SignalKind.CALL_TO_ACTION in kinds, "«получить информацию» не призыв"
    assert SignalKind.OBFUSCATION in kinds, "подмена букв не отмечена"


async def test_job_spam_is_deleted_by_the_dispatcher() -> None:
    from tests.test_dispatcher_e2e import USER_ID, make_harness, make_message

    harness = await make_harness()
    calls = await harness.feed(make_message(JOB_SPAM, USER_ID))
    assert "DeleteMessage" in calls, calls


# --------------------------------------------------------------------------- #
#                       Обратная сторона: ложные срабатывания                  #
# --------------------------------------------------------------------------- #

#: Обычные сообщения, в которых есть те же приметы по отдельности.
LEGIT: tuple[str, ...] = (
    "\u2764\uFE0F обожаю эту книгу, перечитываю каждый год",
    "Ищу книги по σ-алгебрам и α-частицам, посоветуйте учебник",
    "Книга с маркировкой «возраст от 18», кто-нибудь читал?",
    "Зарплата у переводчиков книг — тысяч 60, но это не ежедневно конечно",
    "Получить информацию о книге можно на фантлабе",
    "6\uFE0F\u20E3 томов собрания сочинений, отдам даром",
)


def test_emoji_variation_selector_is_not_obfuscation() -> None:
    """VS16 стоит в каждом втором обычном эмодзи — обходом он быть не может."""
    assert not TextCleaner.normalize("\u2764\uFE0F книга \U0001F642").obfuscated


def test_greek_letters_in_math_are_not_obfuscation() -> None:
    """Отдельно стоящая греческая буква — математика, а не подмена."""
    assert not TextCleaner.normalize("σ-алгебра и α-частицы").obfuscated


def test_book_chat_is_not_touched() -> None:
    for text in LEGIT:
        verdict = _check(text)
        assert verdict.action is Action.ALLOW, f"{text!r} -> {verdict.explain()}"
