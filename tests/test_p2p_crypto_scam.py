"""P2P-скам: «куплю USDT выше рынка» и «куплю US.DT при личной встрече».

Повод: в чат пришли две рассылки, и фильтр пропустил обе. Они не похожи на
прежний спам — в них нет ни ссылок, ни приглашений в канал, ни стоп-слов из
словаря. Весь текст выглядит как частное объявление, а ловушка спрятана в
самом предложении сделки: жертва первой отправляет криптовалюту.

Здесь закреплены оба живых текста и обратная сторона: в читательском чате
говорят и про биткоин, и про встречу «из рук в руки», и страдать это не должно.
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
from bot.services.stopwords import Category  # noqa: E402

SETTINGS = Settings(_env_file=None, bot_token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
ENGINE = AntiSpamEngine(EngineConfig.from_settings(SETTINGS))

#: Точный текст первой рассылки — с эмодзи и переносами посреди слов.
CHINA_SCAM = (
    "I'm from China  \U0001F4B5\U0001F4B5\u2764\u2764\u2764\u2764\U0001F4B5\U0001F4B5\u2764\u2764\n"
    "and am currently unable to purchase USDT due to policy restrictions.\n"
    " I'm now buying USDT at 5%-20% above the market price. (No need to scan QR cod\n"
    "es or visit unfamiliar links.) Once we confirm you have\n"
    " the USDT, we'll make the payment first, and then you can send the \n"
    "USDT to me! Telegram:  @YLPAY_868"
)

#: Вторая рассылка: точка внутри «US.DT» и латинская «c» в «курc».
MEETUP_SCAM = "Приветствую. Куплю US.DT при личной встрече, гарантирую выгодный курc. Пишите"

#: Варианты обхода, которые обязаны ловиться так же.
OBFUSCATED: tuple[str, ...] = (
    "Куплю U.S.D.T при личной встрече, гарантирую выгодный курс",
    "куплю u s d t по выгодному курсу, из рук в руки, деньги вперед",
    "Покупаю ЮСДТ 💰 при личной встрече, курс выше биржевого",
    "Buying USDT at 10% above market price, no need to scan QR codes",
    "I am from China, cannot purchase USDT. We pay first, then you send the USDT to me",
    "Куплю крипту за наличные при личной встрече, работаю через гаранта",
)

#: Обычная жизнь читательского чата: про крипту говорят, книги передают лично.
LEGIT: tuple[str, ...] = (
    "Ищу Mastering Bitcoin Антонопулоса, готов купить при личной встрече",
    "Отдам книгу про биткоин даром, из рук в руки, я в центре",
    "Кто читал книги про крипту? Посоветуйте что-нибудь толковое",
    "Обсуждали вчера крипту, интересная тема для нон-фикшна",
    "Ищу книгу «Цифровое золото», можно epub или при личной встрече забрать",
    "Продам бумажную книгу по блокчейну, встретимся лично у метро",
    "Подскажите книгу про то, как устроен курс биткоина",
    "Передам учебник при личной встрече, пишите в личку",
)


def _check(text: str, **kwargs: object) -> Verdict:
    return ENGINE.evaluate(MessageContext(text=text, **kwargs))  # type: ignore[arg-type]


def test_china_usdt_scam_is_caught() -> None:
    """Живая рассылка про Китай удаляется, а не просто набирает баллы."""
    verdict = _check(CHINA_SCAM)
    assert verdict.action.requires_delete, verdict.explain()
    assert Category.CRYPTO_SCAM in verdict.categories
    assert any(s.kind is SignalKind.SCAM_OFFER for s in verdict.signals), verdict.explain()


def test_meetup_scam_is_caught_without_any_link() -> None:
    """Вторая рассылка ловится без единой ссылки и без упоминаний."""
    verdict = _check(MEETUP_SCAM)
    assert verdict.action.requires_delete, verdict.explain()
    assert Category.CRYPTO_SCAM in verdict.categories


def test_scam_offer_is_a_hard_factor_on_its_own() -> None:
    """Оффер сам по себе — спам-фактор: второго признака схеме не требуется.

    Это исключение из главного инварианта движка, и оно должно быть видно
    в тесте: «куплю USDT при личной встрече» не тема разговора, а предложение.
    """
    verdict = _check("Куплю USDT при личной встрече, гарантирую выгодный курс")
    assert verdict.hard_factors, verdict.explain()
    assert verdict.action.requires_delete, verdict.explain()


def test_obfuscated_variants_are_caught() -> None:
    for text in OBFUSCATED:
        verdict = _check(text)
        assert verdict.action.requires_delete, f"{text!r} -> {verdict.explain()}"


def test_book_chat_is_not_touched() -> None:
    """Ни одно обычное сообщение читательского чата не должно пострадать."""
    for text in LEGIT:
        verdict = _check(text)
        assert verdict.action is Action.ALLOW, f"{text!r} -> {verdict.explain()}"


def test_crypto_word_alone_is_still_harmless() -> None:
    """Гейт `requires` работает в обе стороны: половина схемы — не схема."""
    assert _check("Куплю книгу при личной встрече").action is Action.ALLOW
    assert _check("USDT сегодня подрос").action is Action.ALLOW
    assert _check("Встретимся лично, отдам сборник рассказов").action is Action.ALLOW


def test_telegram_contact_line_is_detected() -> None:
    """`Telegram: @user` — контакт. Раньше эта альтернатива была мертва."""
    verdict = _check(CHINA_SCAM)
    assert any(s.kind is SignalKind.CONTACT for s in verdict.signals), verdict.explain()


def test_scam_offer_does_not_earn_trust() -> None:
    """Даже если оффер проскочил, автор не приближается к «одобренным»."""
    verdict = _check("Ищу Mastering Bitcoin, куплю при личной встрече")
    assert verdict.action is Action.ALLOW, verdict.explain()
    assert verdict.has_promo, verdict.explain()


# --------------------------------------------------------------------------- #
#        Сквозная проверка: сообщение действительно удаляется из чата          #
# --------------------------------------------------------------------------- #


async def test_china_scam_is_deleted_by_the_dispatcher() -> None:
    """Вердикт — половина дела. Здесь апдейт идёт через настоящий диспетчер."""
    from tests.test_dispatcher_e2e import USER_ID, make_harness, make_message

    harness = await make_harness()
    calls = await harness.feed(make_message(CHINA_SCAM, USER_ID))
    assert "DeleteMessage" in calls, calls


async def test_meetup_scam_is_deleted_by_the_dispatcher() -> None:
    from tests.test_dispatcher_e2e import USER_ID, make_harness, make_message

    harness = await make_harness()
    calls = await harness.feed(make_message(MEETUP_SCAM, USER_ID))
    assert "DeleteMessage" in calls, calls


async def test_book_request_with_meetup_survives_the_dispatcher() -> None:
    """Обратная сторона: читатель с «куплю при личной встрече» не страдает."""
    from tests.test_dispatcher_e2e import USER_ID, make_harness, make_message

    harness = await make_harness()
    calls = await harness.feed(
        make_message("Ищу Mastering Bitcoin, куплю при личной встрече", USER_ID)
    )
    assert "DeleteMessage" not in calls, calls
