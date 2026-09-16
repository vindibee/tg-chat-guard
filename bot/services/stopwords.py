"""Категориальный словарь стоп-слов и контекстных маркеров.

Словарь намеренно вынесен в отдельный модуль: правила меняются часто, а движок
детекции (`antispam_engine`) — почти никогда. Любое правило можно добавить, не
трогая логику.

Важно: паттерны компилируются ПОСЛЕ прогона через `TextCleaner.base_form`,
поэтому их можно писать «по-человечески» — нормализатор приводит и паттерн, и
текст к одной форме (в частности `ё -> е`, `й -> и`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from bot.services.text_cleaner import NormalizedText, TextCleaner

__all__ = [
    "Category",
    "KeywordRule",
    "KeywordMatch",
    "StopWordRegistry",
    "DEFAULT_REGISTRY",
    "DEFAULT_RULES",
    "SOFT_CATEGORIES",
    "CALL_TO_ACTION_RE",
    "REFERRAL_RE",
    "BOOK_INTENT_RE",
    "SALES_RE",
    "compile_alternation",
]


class Category(StrEnum):
    """Категории тематических правил."""

    CASINO = "casino"
    CRYPTO_SCAM = "crypto_scam"
    ADULT = "adult"
    JOB_SCAM = "job_scam"
    DRUGS = "drugs"
    #: Торговля аккаунтами, номерами, накруткой — отдельный вид рассылок,
    #: почти всегда англоязычный.
    ACCOUNT_TRADE = "account_trade"
    #: Нейтральная финансовая/криптовалютная лексика. Сама по себе НИКОГДА не
    #: является поводом для наказания — это лексика легальных книжных запросов
    #: («Майстеринг Биткоин», «книги по трейдингу», «Криптография»).
    FINANCE_NEUTRAL = "finance_neutral"


#: Категории, сработка которых без спам-фактора не имеет веса вовсе.
SOFT_CATEGORIES: Final[frozenset[Category]] = frozenset({Category.FINANCE_NEUTRAL})


def compile_pattern(source: str, *, boundary: bool = True) -> re.Pattern[str]:
    """Компилирует паттерн в той же нормальной форме, что и текст сообщения."""
    normalized = TextCleaner.base_form(source)
    body = rf"(?<![0-9a-zа-я]){normalized}" if boundary else normalized
    return re.compile(body, re.IGNORECASE | re.UNICODE)


def compile_alternation(
    sources: Sequence[str], *, boundary: bool = True
) -> re.Pattern[str]:
    """Собирает список паттернов в одно регулярное выражение.

    Десяток отдельных `search` по одному и тому же тексту — это десяток проходов.
    Альтернация даёт один проход и на длинных сообщениях экономит кратно:
    словарь из 140 паттернов сжимается до 12 выражений (по одному на правило).
    """
    body = "|".join(f"(?:{TextCleaner.base_form(source)})" for source in sources)
    prefix = r"(?<![0-9a-zа-я])" if boundary else ""
    return re.compile(f"{prefix}(?:{body})", re.IGNORECASE | re.UNICODE)


@dataclass(frozen=True, slots=True)
class KeywordRule:
    """Одно тематическое правило словаря.

    Attributes:
        name: техническое имя (попадает в логи и статистику).
        category: категория правила.
        weight: базовый вклад в спам-скор.
        pattern: все выражения правила, собранные в одну альтернацию.
        check_squashed: проверять ли склеенную форму (анти-обход символами).
        min_squash_len: минимальная длина совпадения в склеенной форме —
            защита от ложных срабатываний на случайных стыках слов.
    """

    name: str
    category: Category
    weight: float
    pattern: re.Pattern[str]
    check_squashed: bool = True
    min_squash_len: int = 6

    @classmethod
    def build(
        cls,
        name: str,
        category: Category,
        weight: float,
        patterns: Sequence[str],
        *,
        check_squashed: bool = True,
        min_squash_len: int = 6,
    ) -> KeywordRule:
        return cls(
            name=name,
            category=category,
            weight=weight,
            pattern=compile_alternation(patterns),
            check_squashed=check_squashed,
            min_squash_len=min_squash_len,
        )


@dataclass(frozen=True, slots=True)
class KeywordMatch:
    """Факт срабатывания правила."""

    rule: KeywordRule
    evidence: str
    obfuscated: bool = False

    @property
    def category(self) -> Category:
        return self.rule.category

    @property
    def weight(self) -> float:
        return self.rule.weight


# --------------------------------------------------------------------------- #
#                                  СЛОВАРЬ                                     #
# --------------------------------------------------------------------------- #

_CASINO: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "casino_brands",
        Category.CASINO,
        4.5,
        [
            r"1x ?(bet|бет|став)", r"mel ?bet", r"pin ?-? ?up", r"vavada", r"вавада",
            r"joy ?casino", r"дж(о|е)и ?казино", r"мостбет", r"most ?bet",
            r"казино ?вулкан", r"vulkan ?casino", r"leon ?bets?", r"париматч",
            r"olimp ?bet", r"фонбет", r"riobet", r"selector ?casino",
        ],
    ),
    KeywordRule.build(
        "casino_core",
        Category.CASINO,
        4.0,
        [
            r"казино", r"casino", r"игровые? автомат", r"онлаин ?слот", r"фриспин",
            r"free ?spin", r"бездепозитн\w* бонус", r"джекпот", r"jackpot",
            r"букмекерск\w* контор", r"ставки на спорт", r"экспресс дня",
        ],
    ),
    KeywordRule.build(
        "casino_soft",
        Category.CASINO,
        2.0,
        [r"рулетк", r"покер ?рум", r"лудоман", r"беттинг", r"кэшбэк на депозит"],
    ),
)

_CRYPTO_SCAM: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "crypto_profit_promise",
        Category.CRYPTO_SCAM,
        4.5,
        [
            r"гарантированн\w* (доход|прибыл|заработок)", r"доход от \d+ ?(%|процент)",
            r"х ?[2-9]\d* (за|в) (ден|недел|мес|сутк)",
            r"удво\w+ ваш\w* (депозит|биткоин|вклад)",
            r"заработок на кри?пт", r"пассивныи доход на кри?пт",
            r"инвестиции с гарантие", r"без риска и вложени",
            r"окупаемость за \d+ ?(ден|недел)",
        ],
    ),
    KeywordRule.build(
        "crypto_scam_schemes",
        Category.CRYPTO_SCAM,
        4.0,
        [
            r"памп(ы|ов|им|группа)", r"pump ?(and|&) ?dump", r"инсаидерск\w* сигнал",
            r"приватн\w* канал\w* сигнал", r"сигналы на (треидинг|вход|фьючерс)",
            r"раздача (крипты|токенов|btc|usdt)", r"give ?away (btc|usdt|crypto)",
            r"airdrop бесплатн", r"бесплатн\w* airdrop", r"обменник с бонусом",
            r"сид ?фраз", r"seed ?phrase", r"верифицируи кошел",
            r"подключи кошел\w* и получ", r"дроп\w* кошельк",
        ],
    ),
    KeywordRule.build(
        "crypto_scam_soft",
        Category.CRYPTO_SCAM,
        2.5,
        [r"скальпинг ?сигнал", r"копи ?треидинг", r"арбитражн\w* связк", r"p2p связк"],
    ),
)

_ADULT: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "adult_hard",
        Category.ADULT,
        4.5,
        [
            r"порнух", r"порно ?(бот|канал|видео|чат|ссылк)", r"porn ?(bot|hub|video)",
            r"хентаи", r"hentai", r"intim ?(bot|услуг)",
            r"интим ?(услуг|досуг|встреч)", r"секс ?(бот|знакомств|услуг|досуг|за деньги)",
            r"слив\w* (нюдс|интим|приват)", r"нюдс", r"nudes", r"onlyfans", r"онлифанс",
            r"вирт ?(по|за) ?(деньги|подписк)", r"эскорт услуг", r"xxx ?видео",
        ],
    ),
    KeywordRule.build(
        "adult_promo",
        Category.ADULT,
        3.0,
        [
            r"18 ?\+ ?(контент|канал|бот)", r"горяч\w* фото",
            r"мои приватн\w* (фото|видео|канал)",
            r"эротическ\w* (канал|бот|фото)", r"откровенн\w* фото", r"без цензуры бот",
        ],
    ),
    KeywordRule.build(
        "adult_video_ru",
        Category.ADULT,
        4.5,
        [
            # «Горячие видео в боте», «слив фото девушек», «видео 18+».
            r"горяч\w* (видео|видос|девушк|девочк|малышк|контент|ролик)",
            r"(интим|эротик|пикантн)\w* (видео|видос|ролик|контент)",
            r"видео ?18 ?\+", r"18 ?\+ ?(видео|видос|фото|ролик)",
            r"слив\w* (фото|видео) (девушек|девочек|одноклассниц|блогерш)",
            r"(голые|обнаженн\w*) (девушк|девочк|фото|видео)",
            r"секс ?(видео|чат)", r"вебкам\w*",
        ],
    ),
)

_JOB_SCAM: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "job_scam_offer",
        Category.JOB_SCAM,
        4.0,
        [
            r"требуютс?я? (сотрудник|люди|партн(е|ё)р)",
            r"набира(ю|ем) (команду|людеи|сотрудник)",
            r"удал(е|ё)нн\w* работа от \d+", r"работа в интернете",
            r"подработк\w* (в|на) (телефон|интернет)",
            r"доход от \d+ ?(\$|usd|долл|руб|₽|тыс|к)\b",
            r"от \d+ ?(\$|\d{3}) в (ден|сутк|недел)",
            r"без опыта и вложени", r"финансов\w* (пирамид|матриц)",
            r"реферальн\w* программ", r"личныи помощник .{0,20}\d+ ?(\$|руб)",
            r"куратор обучени", r"наставник .{0,15}заработ",
        ],
    ),
    KeywordRule.build(
        "job_scam_soft",
        Category.JOB_SCAM,
        2.0,
        [r"быстрыи заработок", r"л(е|ё)гкии заработок", r"пассивныи доход", r"доп ?доход"],
    ),
)

_DRUGS: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "drugs_hard",
        Category.DRUGS,
        6.0,
        [
            r"мефедрон", r"амфетамин", r"мдма", r"\bmdma\b", r"гашиш",
            r"шишки ?(и|,)? ?бошки", r"закладк\w*", r"клады по городу",
            r"соль ?кристалл", r"альфа ?-? ?пвп", r"\ba-?pvp\b",
        ],
    ),
)

#: Нейтральная лексика: нужна только чтобы понять тему сообщения.
#: Вес символический, наказание по ней невозможно (см. `SOFT_CATEGORIES`).
_FINANCE_NEUTRAL: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "finance_terms",
        Category.FINANCE_NEUTRAL,
        0.5,
        [
            r"кри?пт(а|ы|у|е|ои|овалют\w*)", r"crypto", r"биткоин\w*", r"bit ?coin",
            r"блокчеин", r"block ?chain", r"эфириум", r"ethereum", r"usdt", r"nft",
            r"треидинг", r"trading", r"финанс\w*", r"инвестици\w*", r"investment",
            r"заработок", r"заработать", r"бизнес", r"business", r"forex", r"форекс",
            r"фондов\w* рынок", r"экономик\w*", r"маининг", r"mining",
            r"криптографи\w*", r"криптограф", r"шифровани\w*",
            r"passive income", r"financial freedom", r"work from home",
            r"investment plan", r"side hustle", r"stock market",
        ],
        min_squash_len=7,
    ),
)

# --------------------------------------------------------------------------- #
#                        АНГЛОЯЗЫЧНЫЕ ПРАВИЛА                                  #
# --------------------------------------------------------------------------- #
# Русский чат — не помеха для англоязычных рассылок: «telegram accounts cheap
# store, get yours @bot» не содержит ни одного русского слова. Держим их
# отдельными правилами, чтобы в логах было видно, чем именно поймали.

_ACCOUNT_TRADE: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "account_trade_en",
        Category.ACCOUNT_TRADE,
        4.5,
        [
            r"telegram accounts", r"tg accounts", r"accounts? (store|shop|market|service)",
            r"(cheap|aged|verified|bulk|fresh|old) accounts",
            r"(sell|selling|buy|buying) accounts", r"account (store|shop|seller)",
            r"virtual numbers?", r"sms activation", r"proxy (store|shop|seller)",
            r"mass (dm|dms|sending|mailing)", r"auto ?sender", r"smm panel",
            r"buy (views|subs|subscribers|likes|followers)",
            r"boost (your )?(views|subscribers|channel)",
        ],
    ),
    KeywordRule.build(
        "account_trade_ru",
        Category.ACCOUNT_TRADE,
        4.5,
        [
            r"аккаунт(ы|ов) (оптом|дешево|под ключ|в наличии)",
            r"магазин аккаунтов", r"продажа аккаунтов", r"прокачанн\w* аккаунт",
            r"виртуальн\w* номер", r"смс ?активаци",
            r"накрутк\w* (подписчик|просмотр|лаик|реакци)",
        ],
    ),
)

_ENGLISH_SCAM: Final[Sequence[KeywordRule]] = (
    KeywordRule.build(
        "casino_en",
        Category.CASINO,
        4.0,
        [
            r"betting (site|shop)", r"free bets?", r"sports betting", r"online slots",
            r"deposit bonus", r"no deposit", r"win real money",
        ],
    ),
    KeywordRule.build(
        "crypto_scam_en",
        Category.CRYPTO_SCAM,
        4.0,
        [
            r"air ?drop", r"free crypto", r"double your (btc|bitcoin|crypto|money)",
            r"trading signals", r"pump group", r"seed phrase",
            r"wallet (verification|validation)", r"connect (your )?wallet",
            r"guaranteed (profit|returns?)", r"crypto giveaway",
        ],
    ),
    KeywordRule.build(
        "adult_en",
        Category.ADULT,
        4.5,
        [
            # «hot videos», «adult content» и прочие «прилагательное + видео»
            # живут в `adult_video_en` — здесь их нет, чтобы не считать дважды.
            r"sex chat", r"leaked (nudes|photos)", r"webcam girls", r"dating for sex",
        ],
    ),
    KeywordRule.build(
        "adult_bait_en",
        Category.ADULT,
        3.0,
        [
            # Приманка без явной лексики 18+: «check BEST vide0s and meet girls on
            # this bot», «ye bot me real videos h». Вес ниже, чем у adult_video_en:
            # сама по себе фраза не криминальна, спамом её делает бот рядом.
            # Прилагательные подобраны узко: «new video», «free videos», «best
            # videos» — обычный разговор, их здесь нет.
            r"(meet|find|date|chat with|talk to) (\w+ )?(girls?|women|ladies|singles|babes)",
            r"(real|exclusive|viral|secret|unseen|hidden|desi) (videos?|vids?|clips?|mms)",
            r"(girls?|ladies|bhabhi) (videos?|vids?|pics?)", r"videos? (and|&|\+) girls?",
            r"viral mms", r"desi (girls?|bhabhi)",
        ],
    ),
    KeywordRule.build(
        "adult_video_en",
        Category.ADULT,
        4.5,
        [
            # Завуалированная реклама: «super h0t vide0s avaible on this bot».
            # Leet и гомоглифы снимает нормализатор, здесь — только лексика.
            r"hot (girls?|babes?|videos?|vids?|pics?|photos?|content|chat|clips?|stuff)",
            r"(sexy|horny|naughty|naked|nude) (girls?|babes?|videos?|vids?|pics?|photos?"
            r"|content|chat|singles?|teens?)",
            # Правило не должно пересекаться с `adult_hard` и `adult_en`:
            # одно и то же выражение, пойманное двумя правилами, удваивает вес.
            r"(adult|xxx|nsfw|18 ?\+|erotic\w*|spicy|private|uncensored)"
            r" (videos?|vids?|content|pics?|photos?|clips?|channel|bot|chat)",
            r"nsfw", r"cam ?girls?", r"sex ?(videos?|bot|tape)", r"hookups?",
        ],
    ),
    KeywordRule.build(
        "job_scam_en",
        Category.JOB_SCAM,
        4.0,
        [
            # Только однозначно рекламные обороты. «work from home»,
            # «passive income» и «financial freedom» сюда не входят: это
            # названия книг и обычный разговор в читательском чате.
            r"earn \$? ?\d+ ?(a|per|\/) ?(day|week|hour)",
            r"\$ ?\d+ ?(a|per|\/) ?(day|week)",
            r"work from home.{0,25}(\$|earn|income|salary|pay)",
            r"make money (online|fast|while)", r"no experience needed",
            r"hiring (now|remote|urgently)", r"easy money guaranteed",
            r"be your own boss", r"start earning today",
        ],
    ),
)


DEFAULT_RULES: Final[tuple[KeywordRule, ...]] = (
    *_CASINO,
    *_CRYPTO_SCAM,
    *_ADULT,
    *_JOB_SCAM,
    *_DRUGS,
    *_ACCOUNT_TRADE,
    *_ENGLISH_SCAM,
    *_FINANCE_NEUTRAL,
)

# --------------------------------------------------------------------------- #
#                          КОНТЕКСТНЫЕ МАРКЕРЫ                                 #
# --------------------------------------------------------------------------- #

#: Призывы к действию — ключевой «жёсткий» спам-фактор.
CALL_TO_ACTION_RE: Final[re.Pattern[str]] = compile_alternation(
    (
        r"перех(оди|одите|одь) по ссылк", r"жми (на )?(ссылк|кнопк)", r"кликаи (по|на)",
        r"пиши(те)? (мне )?(в )?(лс|личку|личные сообщени|директ)",
        r"напиши(те)? (мне )?в (лс|личку|директ)",
        r"\+ ?в (лс|личку|коммент)", r"подробности в (лс|личку|директ)",
        r"кому интересно,? пиш", r"кому надо,? пиш", r"по всем вопросам пиш",
        r"начни(те)? зарабатывать", r"хочешь зарабатывать",
        r"зарабатываи (уже )?(сеичас|сегодня)", r"начни зарабатывать прямо сеичас",
        r"регистрируися по", r"переходи и получи", r"забираи (бонус|подарок|доступ)",
        r"успеи(те)? (до|заб|зар)", r"только сегодня", r"ограниченн\w* предложени",
        r"осталось \d+ мест", r"набор закрывается", r"пиши слово", r"ставь ?\+",
        r"подпишись (на|и)", r"вступаи в", r"переходи в (бот|канал)", r"жду в лс",
        r"научу зарабатывать", r"пишите в телеграм", r"свяжись со мнои",
        # Реклама через бота: сам текст пустой, всё «содержимое» — в боте.
        # «Пиши в бота» и «ищи в боте» сюда сознательно не входят: так в
        # книжном чате объясняют, как пользоваться Флибустой.
        r"(переходи|заходи|залетаи|запускаи)(те)? (в|на) бот\w*",
        r"в (нашем|моем) боте", r"(смотри|забираи)(те)? в боте",
        r"(ссылка|доступ|видео|фото) в боте", r"жми (старт|start)",
        # Англоязычные призывы: рассылки часто вообще не содержат русских слов.
        r"get yours?", r"click (here|the link|below)", r"(dm|pm|text|message) me",
        r"contact me", r"hit me up", r"write (to )?me", r"join (now|us|our)",
        r"check (my )?bio", r"link in bio", r"(order|buy|shop|sign up|register) now",
        r"limited (offer|time)", r"act now", r"don'?t miss", r"free trial",
        r"start earning", r"tap (the )?link", r"more info in",
        # «...avaible on this bot», «check my bot», «start the bot».
        r"(av[a-z]{2,5}ble|here|inside|waiting|free) (on|in|at|via) (this|my|our|the) bot",
        r"(on|in|via) (my|our) bot", r"(start|open|check) (my|our) bot",
        r"go to (my|our) bot", r"(watch|see) (it |them |more )?(in|on) (the |my |our )?bot",
    )
)

#: Реферальные/промо-маркеры.
REFERRAL_RE: Final[re.Pattern[str]] = compile_alternation(
    (
        r"[?&](ref|refid|referral|invite|promo|utm_source|partner|aff|affiliate)=",
        r"/ref/", r"/invite/", r"промокод", r"promo ?code", r"реферальн\w* ссылк",
        r"по мое(и|му) (ссылк|реф)", r"invite ?code", r"бонус ?-? ?код",
    ),
    boundary=False,
)

#: Маркеры книжного/поискового намерения — основной анти-FP сигнал.
BOOK_INTENT_RE: Final[re.Pattern[str]] = compile_alternation(
    (
        r"книг\w*", r"книжк\w*", r"аудиокниг\w*", r"учебник\w*", r"пособи\w*",
        r"автор\w*", r"писател\w*", r"роман\w*", r"повест(ь|и)", r"расск(аз|азы|азов)",
        r"монографи\w*", r"издани\w*", r"издательств\w*", r"серия", r"том \d+",
        r"скача(и|ть|л|ла)", r"скачива", r"почита(ть|и|ю|л)", r"чита(ю|л|ла|ть|ем|ет)",
        r"прочит(ал|ала|ать)", r"дочита", r"перечита", r"полистат",
        r"ищу", r"поищ", r"наид(и|ите|у|ется|ется ли)", r"не могу наити",
        r"подскажите", r"посоветуите", r"посоветуи", r"порекомендуите", r"ищется",
        r"есть ли (у кого|в наличии|тут|здесь)", r"кто читал", r"что почитать",
        r"\bfb2\b", r"\bepub\b", r"\bmobi\b", r"\bdjvu\b", r"\bpdf\b", r"\btxt\b",
        r"библиотек\w*", r"флибуст\w*", r"litres", r"литрес", r"фантлаб", r"либген",
        r"жанр\w*", r"фантастик\w*", r"детектив\w*", r"нон ?-? ?фикшн", r"публицистик",
        r"перевод\w*", r"оригинал\w* (издани|на англ)", r"в бумаге", r"бумажн\w* издани",
        r"саммари", r"аннотаци", r"оглавлени", r"глава \d+", r"страниц",
        r"учебн\w* по", r"справочник", r"курс лекции", r"мануал", r"руководство по",
    )
)

#: Маркеры коммерческого предложения — их поиском книги не оправдать.
SALES_RE: Final[re.Pattern[str]] = compile_alternation(
    (
        r"продам", r"продаю", r"куплю дорого", r"опт(ом)? и в розниц",
        r"цена от \d+", r"скидка \d+ ?%", r"успеи купить", r"оформить заказ",
    )
)


class StopWordRegistry:
    """Реестр правил с поиском совпадений по нормализованному тексту."""

    def __init__(self, rules: Iterable[KeywordRule] = DEFAULT_RULES) -> None:
        self._rules: tuple[KeywordRule, ...] = tuple(rules)
        # Быстрый предфильтр: «есть ли вообще хоть одно стоп-слово».
        # Чистых сообщений в живом чате подавляющее большинство, и для них
        # это один проход по тексту вместо прохода на каждое правило.
        self._any_rule: re.Pattern[str] = re.compile(
            "|".join(f"(?:{rule.pattern.pattern})" for rule in self._rules),
            re.IGNORECASE | re.UNICODE,
        )

    @property
    def rules(self) -> tuple[KeywordRule, ...]:
        return self._rules

    def match(self, text: NormalizedText) -> tuple[KeywordMatch, ...]:
        """Возвращает по одному совпадению на каждое сработавшее правило."""
        if text.search(self._any_rule) is None and text.search_squashed(self._any_rule) is None:
            return ()
        matches: list[KeywordMatch] = []
        for rule in self._rules:
            found = self._match_rule(rule, text)
            if found is not None:
                matches.append(found)
        return tuple(matches)

    @staticmethod
    def _match_rule(rule: KeywordRule, text: NormalizedText) -> KeywordMatch | None:
        found = text.search(rule.pattern)
        if found is not None:
            return KeywordMatch(rule=rule, evidence=found.group(0))
        if not rule.check_squashed:
            return None
        found = text.search_squashed(rule.pattern)
        if found is not None and len(found.group(0)) >= rule.min_squash_len:
            # Совпадение только в «склеенной» форме = попытка обхода фильтра.
            return KeywordMatch(rule=rule, evidence=found.group(0), obfuscated=True)
        return None


DEFAULT_REGISTRY: Final[StopWordRegistry] = StopWordRegistry()

