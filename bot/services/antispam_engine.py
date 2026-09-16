"""Движок умной детекции спама с защитой от ложных срабатываний.

Главный инвариант модуля:

    Стоп-слово САМО ПО СЕБЕ никогда не является основанием для наказания.
    Санкция применяется, только если тематическая лексика совмещена
    хотя бы с одним «жёстким» спам-фактором: внешней ссылкой, приглашением
    в Telegram, реферальным кодом, призывом к действию или упоминанием
    постороннего канала.

Благодаря этому книжные запросы вида «Майстеринг Биткоин», «Криптография»,
«книги по трейдингу» или «Секс в большом городе» проходят фильтр свободно.

Модуль не зависит от aiogram и от pydantic — это чистая логика над текстом,
пригодная для юнит-тестов и переиспользования.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from bot.services.stopwords import (
    BOOK_INTENT_RE,
    CALL_TO_ACTION_RE,
    DEFAULT_REGISTRY,
    REFERRAL_RE,
    SALES_RE,
    SOFT_CATEGORIES,
    Category,
    KeywordMatch,
    StopWordRegistry,
)
from bot.services.text_cleaner import NormalizedText, TextCleaner

if TYPE_CHECKING:  # pragma: no cover
    from bot.config import Settings

__all__ = [
    "Action",
    "AntiSpamEngine",
    "EngineConfig",
    "MessageContext",
    "Signal",
    "SignalKind",
    "Verdict",
    "VerdictReason",
]


class SignalKind(StrEnum):
    """Тип обнаруженного признака."""

    KEYWORD = "keyword"
    LINK = "link"
    TELEGRAM_INVITE = "telegram_invite"
    HIDDEN_LINK = "hidden_link"
    MENTION = "mention"
    #: Упоминание бота (`@..._bot`, `t.me/...bot`) — основной канал рекламы
    #: спам-сеток: сам текст безобиден, всё «содержимое» спрятано в боте.
    BOT_MENTION = "bot_mention"
    REFERRAL = "referral"
    CALL_TO_ACTION = "call_to_action"
    SALES_PITCH = "sales_pitch"
    CONTACT = "contact"
    OBFUSCATION = "obfuscation"
    FORWARD = "forward"
    NEW_MEMBER = "new_member"
    #: Автор найден в публичном реестре спамеров (CAS/LOLS).
    EXTERNAL_BAN = "external_ban"
    #: Реклама вынесена в имя профиля или username.
    PROFILE_KEYWORD = "profile_keyword"
    #: Ссылка или инвайт прямо в имени профиля.
    PROFILE_LINK = "profile_link"
    #: Один и тот же текст прислали несколько разных участников.
    DUPLICATE = "duplicate"


#: Признаки, превращающие «тему» в «спам». Без хотя бы одного из них
#: движок не наказывает (при `require_spam_factor=True`).
HARD_FACTORS: Final[frozenset[SignalKind]] = frozenset(
    {
        SignalKind.LINK,
        SignalKind.TELEGRAM_INVITE,
        SignalKind.HIDDEN_LINK,
        SignalKind.MENTION,
        SignalKind.BOT_MENTION,
        SignalKind.REFERRAL,
        SignalKind.CALL_TO_ACTION,
        SignalKind.CONTACT,
        SignalKind.EXTERNAL_BAN,
        SignalKind.PROFILE_KEYWORD,
        SignalKind.PROFILE_LINK,
        SignalKind.DUPLICATE,
    }
)


class Action(StrEnum):
    """Итоговое решение по сообщению."""

    ALLOW = "allow"
    DELETE = "delete"
    DELETE_AND_MUTE = "delete_and_mute"
    DELETE_AND_BAN = "delete_and_ban"

    @property
    def is_punishment(self) -> bool:
        return self is not Action.ALLOW

    @property
    def requires_delete(self) -> bool:
        return self is not Action.ALLOW


class VerdictReason(StrEnum):
    """Причина решения — попадает в структурированный лог."""

    CLEAN = "clean"
    EMPTY = "empty"
    NO_SPAM_FACTOR = "no_spam_factor"
    SEARCH_QUERY = "search_query"
    SAFE_DOMAIN_ONLY = "safe_domain_only"
    BELOW_THRESHOLD = "below_threshold"
    SPAM_DETECTED = "spam_detected"
    PROHIBITED_CATEGORY = "prohibited_category"


@dataclass(frozen=True, slots=True)
class Signal:
    """Один найденный признак с его вкладом в скор."""

    kind: SignalKind
    weight: float
    evidence: str
    category: Category | None = None

    @property
    def is_hard(self) -> bool:
        return self.kind in HARD_FACTORS

    def __str__(self) -> str:  # pragma: no cover - для логов
        label = f"{self.kind.value}" + (f"/{self.category.value}" if self.category else "")
        return f"{label}(+{self.weight:g}: {self.evidence[:48]!r})"


@dataclass(frozen=True, slots=True)
class Verdict:
    """Результат проверки сообщения."""

    action: Action
    score: float
    reason: VerdictReason
    signals: tuple[Signal, ...] = ()
    categories: frozenset[Category] = frozenset()
    book_intent: bool = False

    @property
    def is_spam(self) -> bool:
        return self.action.is_punishment

    @property
    def hard_factors(self) -> tuple[Signal, ...]:
        return tuple(signal for signal in self.signals if signal.is_hard)

    def explain(self) -> str:
        """Человекочитаемое объяснение для логов и админ-уведомлений."""
        parts = [
            f"action={self.action.value}",
            f"score={self.score:.2f}",
            f"reason={self.reason.value}",
        ]
        if self.categories:
            parts.append("categories=" + ",".join(sorted(c.value for c in self.categories)))
        if self.book_intent:
            parts.append("book_intent=1")
        if self.signals:
            parts.append("signals=[" + "; ".join(str(s) for s in self.signals) + "]")
        return " ".join(parts)

    def as_log_extra(self) -> dict[str, object]:
        """Поля для структурированного лога."""
        return {
            "verdict_action": self.action.value,
            "verdict_score": round(self.score, 2),
            "verdict_reason": self.reason.value,
            "verdict_categories": sorted(c.value for c in self.categories),
            "verdict_signals": [s.kind.value for s in self.signals],
            "book_intent": self.book_intent,
        }

    @classmethod
    def allowed(cls, reason: VerdictReason, **kwargs: object) -> Verdict:
        return cls(action=Action.ALLOW, score=0.0, reason=reason, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MessageContext:
    """Нормализованное представление сообщения для движка.

    Собирается в хендлере из объекта aiogram — сам движок про aiogram не знает.
    """

    text: str = ""
    #: URL, спрятанные в сущностях (`text_link`) — их нет в тексте сообщения.
    entity_urls: tuple[str, ...] = ()
    #: Упоминания `@username` из сущностей (нижний регистр, без `@`).
    mentions: tuple[str, ...] = ()
    is_forward: bool = False
    forward_from_chat: bool = False
    has_media: bool = False
    #: Пользователь недавно вступил в чат — типичный профиль спамера.
    is_new_member: bool = False
    #: Упоминания, которые не считаются подозрительными (админы, боты чата).
    known_safe_usernames: frozenset[str] = frozenset()
    #: Отображаемое имя автора (или название канала-отправителя).
    author_name: str = ""
    #: Username автора без «@».
    author_username: str = ""
    #: Сколько РАЗНЫХ участников прислали этот же текст в окне наблюдения.
    duplicate_authors: int = 0
    #: Автор найден во внешнем блок-листе.
    external_ban: bool = False
    #: Какой именно источник его нашёл (для логов и карточки).
    external_ban_source: str = ""

    @property
    def profile(self) -> str:
        """Имя и username одной строкой — то, что видно рядом с каждым сообщением."""
        return " ".join(part for part in (self.author_name, self.author_username) if part).strip()

    @property
    def payload(self) -> str:
        """Текст + скрытые URL: проверяем то, что реально видит пользователь и то, что скрыто."""
        if not self.entity_urls:
            return self.text
        return "\n".join((self.text, *self.entity_urls))


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Настройки движка (веса, пороги, исключения)."""

    delete_threshold: float = 6.0
    mute_threshold: float = 9.0
    ban_threshold: float = 13.0
    require_spam_factor: bool = True
    safe_domains: frozenset[str] = frozenset()
    #: Скидка к скору, если сообщение выглядит как книжный/поисковый запрос.
    book_intent_discount: float = 4.0
    #: Со скольких разных авторов одинаковый текст считается рассылкой.
    duplicate_threshold: int = 3
    #: Категории, при которых наличие спам-фактора сразу даёт бан.
    escalate_categories: frozenset[Category] = frozenset({Category.DRUGS})
    weights: Mapping[SignalKind, float] = field(
        default_factory=lambda: {
            SignalKind.LINK: 4.0,
            SignalKind.TELEGRAM_INVITE: 5.0,
            SignalKind.HIDDEN_LINK: 5.0,
            SignalKind.MENTION: 2.0,
            SignalKind.BOT_MENTION: 4.5,
            SignalKind.REFERRAL: 4.0,
            SignalKind.CALL_TO_ACTION: 3.0,
            SignalKind.SALES_PITCH: 2.0,
            SignalKind.CONTACT: 2.5,
            SignalKind.OBFUSCATION: 2.0,
            SignalKind.FORWARD: 1.5,
            SignalKind.NEW_MEMBER: 1.0,
            SignalKind.EXTERNAL_BAN: 5.0,
            SignalKind.PROFILE_KEYWORD: 3.5,
            SignalKind.PROFILE_LINK: 4.0,
            SignalKind.DUPLICATE: 4.0,
        }
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> EngineConfig:
        """Собирает конфиг движка из общих настроек приложения."""
        config = cls(
            delete_threshold=settings.delete_threshold,
            mute_threshold=settings.mute_threshold,
            ban_threshold=settings.ban_threshold,
            require_spam_factor=settings.require_spam_factor,
            safe_domains=frozenset(settings.safe_domains),
            duplicate_threshold=settings.duplicate_threshold,
        )
        weights = dict(config.weights)
        weights[SignalKind.EXTERNAL_BAN] = settings.reputation_weight
        weights[SignalKind.DUPLICATE] = settings.duplicate_weight
        weights[SignalKind.BOT_MENTION] = settings.bot_mention_weight
        return replace(config, weights=weights)

    def weight(self, kind: SignalKind) -> float:
        return self.weights.get(kind, 1.0)


# --------------------------------------------------------------------------- #
#                         РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ                                 #
# --------------------------------------------------------------------------- #

_SCHEME_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|www\.)(?P<host>[a-z0-9.\-]+)(?P<path>[^\s<>\"']*)", re.IGNORECASE
)
#: Домены верхнего уровня, по которым распознаём ссылку без схемы.
_BARE_TLD: Final[str] = (
    "ru|su|рф|com|net|org|info|biz|pro|app|dev|io|cc|me|tv|xyz|top|club|online|site|"
    "store|shop|link|live|vip|win|bet|casino|space|fun|life|world|cyou|icu|monster"
)
_BARE_URL_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![@\w.])(?P<host>(?:[a-z0-9](?:[a-z0-9\-]{{0,61}}[a-z0-9])?\.)+(?:{_BARE_TLD}))"
    rf"(?P<path>/[^\s<>\"']*)?(?![\w.])",
    re.IGNORECASE,
)
_TELEGRAM_INVITE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://)?(?:t(?:elegram)?\.me|telegram\.dog)/(?P<target>\+[\w\-]+|joinchat/[\w\-]+|[\w\-]{4,})",
    re.IGNORECASE,
)
_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w/])@([a-z0-9_]{4,32})", re.IGNORECASE)
#: Цифры, которыми маскируют окончание: «@promo_b0t», «@hot_8ot».
_USERNAME_DELEET: Final[dict[int, str]] = str.maketrans({"0": "o", "8": "b", "7": "t"})


def _is_bot_username(username: str) -> bool:
    """Telegram требует, чтобы username бота кончался на «bot» — этим и пользуемся."""
    name = username.lstrip("@").lower().rstrip("_")
    return len(name) > 3 and name.translate(_USERNAME_DELEET).endswith("bot")


_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s\-()]{0,3})?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_MESSENGER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(whats ?app|вотс ?ап|ватс ?ап|viber|ваибер|вибер|telegram ?:|тг ?:|wa\.me)\b",
    re.IGNORECASE,
)
#: Домены, которые встречаются в обычной речи и ссылкой не являются.
_FALSE_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:т\.е|т\.к|т\.д|т\.п|и\.о|др\.ru)$", re.IGNORECASE
)


def _extract_host(host: str) -> str:
    """Возвращает регистрируемый домен второго уровня (`a.b.example.com` -> `example.com`)."""
    parts = [part for part in host.lower().strip(".").split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    # Учитываем составные зоны вида `co.uk`, `com.ru`.
    if parts[-2] in {"co", "com", "org", "net", "gov", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class AntiSpamEngine:
    """Контекстный анализатор сообщений.

    Порядок работы:
        1. нормализация текста (анти-обфускация);
        2. сбор тематических совпадений по словарю;
        3. сбор контекстных спам-факторов (ссылки, CTA, рефералки, упоминания);
        4. применение анти-FP правил (книжный поиск, доверенные домены);
        5. расчёт скора и выбор санкции по порогам.
    """

    def __init__(
        self,
        config: EngineConfig | None = None,
        registry: StopWordRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self._config = config or EngineConfig()
        self._registry = registry

    @property
    def config(self) -> EngineConfig:
        return self._config

    # ------------------------------------------------------------------ #
    #                            ПУБЛИЧНОЕ API                            #
    # ------------------------------------------------------------------ #

    def check(self, text: str, **kwargs: object) -> Verdict:
        """Упрощённый вход для тестов и ручных проверок."""
        return self.evaluate(MessageContext(text=text, **kwargs))  # type: ignore[arg-type]

    def evaluate(self, context: MessageContext) -> Verdict:
        """Главная точка входа: возвращает вердикт по сообщению."""
        normalized = TextCleaner.normalize(context.payload)
        if normalized.is_empty and not context.entity_urls:
            return Verdict.allowed(VerdictReason.EMPTY)

        keyword_matches = self._registry.match(normalized)
        categories = frozenset(
            match.category for match in keyword_matches if match.category not in SOFT_CATEGORIES
        )
        soft_only = not categories and bool(keyword_matches)

        signals: list[Signal] = []
        profile_signals, profile_categories = self._profile_signals(context)
        signals.extend(profile_signals)
        categories = categories | profile_categories
        signals.extend(self._keyword_signals(keyword_matches))
        link_signals, only_safe_domains = self._link_signals(context, normalized)
        signals.extend(link_signals)
        signals.extend(self._context_signals(context, normalized))
        keyword_obfuscated = any(match.obfuscated for match in keyword_matches)
        signals.extend(
            self._behaviour_signals(
                context, normalized, bool(categories), keyword_obfuscated
            )
        )

        book_intent = self._detect_book_intent(normalized, signals)
        hard = [signal for signal in signals if signal.is_hard]

        # --- Анти-FP слой ------------------------------------------------
        # 1. Нет ни одного жёсткого фактора — это не спам, что бы ни было в тексте.
        if self._config.require_spam_factor and not hard:
            return Verdict(
                action=Action.ALLOW,
                score=self._score(signals),
                reason=VerdictReason.NO_SPAM_FACTOR,
                signals=tuple(signals),
                categories=categories,
                book_intent=book_intent,
            )

        # 2. Единственный «фактор» — ссылка на доверенный книжный ресурс.
        if only_safe_domains and not any(
            signal.kind is not SignalKind.LINK for signal in hard
        ):
            return Verdict(
                action=Action.ALLOW,
                score=self._score(signals),
                reason=VerdictReason.SAFE_DOMAIN_ONLY,
                signals=tuple(signals),
                categories=categories,
                book_intent=book_intent,
            )

        # 3. Книжный поиск с нейтральной финансовой лексикой — не спам.
        if book_intent and soft_only and not self._has_aggressive_factor(hard):
            return Verdict(
                action=Action.ALLOW,
                score=self._score(signals),
                reason=VerdictReason.SEARCH_QUERY,
                signals=tuple(signals),
                categories=categories,
                book_intent=book_intent,
            )

        score = self._score(signals)
        # Книжное намерение смягчает оценку, но не спасает от агрессивных факторов
        # (инвайт в приватный канал, скрытая ссылка, рефералка, прямой призыв).
        if book_intent and not self._has_aggressive_factor(hard):
            score -= self._config.book_intent_discount

        action, reason = self._decide(score, categories, hard)
        return Verdict(
            action=action,
            score=max(score, 0.0),
            reason=reason,
            signals=tuple(signals),
            categories=categories,
            book_intent=book_intent,
        )

    # ------------------------------------------------------------------ #
    #                          СБОР ПРИЗНАКОВ                             #
    # ------------------------------------------------------------------ #

    def _keyword_signals(self, matches: Iterable[KeywordMatch]) -> list[Signal]:
        """Тематические совпадения словаря."""
        return [
            Signal(
                kind=SignalKind.KEYWORD,
                weight=match.weight,
                evidence=match.evidence,
                category=match.category,
            )
            for match in matches
        ]

    def _profile_signals(
        self, context: MessageContext
    ) -> tuple[list[Signal], frozenset[Category]]:
        """Реклама в имени профиля и username.

        Спамеры часто оставляют текст сообщения безобидным, а весь оффер выносят
        в имя: «КАЗИНО ВУЛКАН», «Заработок 24/7», «@promo_casino». Такое имя —
        жёсткий фактор: назвать себя «Казино Вулкан» по ошибке невозможно, в
        отличие от разговора о казино в тексте.

        Нейтральная финансовая лексика в имени игнорируется: «Криптоведьмак»
        и «Биткоин Иваныч» — это просто читатели.
        """
        profile = context.profile
        if not profile:
            return [], frozenset()

        signals: list[Signal] = []
        categories: set[Category] = set()

        link = self._profile_link(profile)
        if link is not None:
            signals.append(
                Signal(
                    kind=SignalKind.PROFILE_LINK,
                    weight=self._config.weight(SignalKind.PROFILE_LINK),
                    evidence=f"{link} (в профиле)",
                )
            )

        normalized = TextCleaner.normalize(profile)
        for match in self._registry.match(normalized):
            if match.category in SOFT_CATEGORIES:
                continue
            categories.add(match.category)
            signals.append(
                Signal(
                    kind=SignalKind.PROFILE_KEYWORD,
                    weight=self._config.weight(SignalKind.PROFILE_KEYWORD),
                    evidence=f"{match.evidence} (в профиле)",
                    category=match.category,
                )
            )
            break  # одного совпадения достаточно, вес не суммируем

        return signals, frozenset(categories)

    def _profile_link(self, profile: str) -> str | None:
        """Ссылка или инвайт в имени профиля, если она ведёт не на доверенный домен."""
        invite = _TELEGRAM_INVITE_RE.search(profile)
        if invite is not None:
            return invite.group(0)
        for pattern in (_SCHEME_URL_RE, _BARE_URL_RE):
            match = pattern.search(profile)
            if match is None:
                continue
            host = _extract_host(match.group("host"))
            if not host or _FALSE_DOMAIN_RE.match(host) or host in self._config.safe_domains:
                continue
            return match.group(0)[:80]
        return None

    def _link_signals(
        self, context: MessageContext, normalized: NormalizedText
    ) -> tuple[list[Signal], bool]:
        """Ссылки, приглашения и скрытые URL.

        Returns:
            Кортеж «список сигналов, все ли найденные ссылки ведут на доверенные домены».
        """
        signals: list[Signal] = []
        hosts_total = 0
        hosts_unsafe = 0

        # 1. Приглашения в Telegram — самый частый носитель спама.
        for match in _TELEGRAM_INVITE_RE.finditer(context.payload):
            target = match.group("target").lower()
            if target.lstrip("+") in context.known_safe_usernames:
                continue
            hosts_total += 1
            hosts_unsafe += 1
            private = target.startswith(("+", "joinchat"))
            if private:
                kind = SignalKind.TELEGRAM_INVITE
            elif _is_bot_username(target):
                kind = SignalKind.BOT_MENTION
            else:
                kind = SignalKind.LINK
            signals.append(
                Signal(kind=kind, weight=self._config.weight(kind), evidence=match.group(0))
            )

        # 2. Обычные ссылки (со схемой и «голые» домены).
        seen_hosts: set[str] = set()
        for pattern in (_SCHEME_URL_RE, _BARE_URL_RE):
            for match in pattern.finditer(context.payload):
                host = _extract_host(match.group("host"))
                if not host or _FALSE_DOMAIN_RE.match(host) or "t.me" in match.group(0).lower():
                    continue
                if host in seen_hosts:
                    continue
                seen_hosts.add(host)
                hosts_total += 1
                if host in self._config.safe_domains:
                    continue
                hosts_unsafe += 1
                signals.append(
                    Signal(
                        kind=SignalKind.LINK,
                        weight=self._config.weight(SignalKind.LINK),
                        evidence=match.group(0)[:120],
                    )
                )

        # 3. Скрытые ссылки: текст показывает одно, сущность ведёт на другое.
        for url in context.entity_urls:
            if not url.strip():
                continue
            match = _SCHEME_URL_RE.search(url)
            host = _extract_host(match.group("host")) if match else ""
            if host and host in self._config.safe_domains:
                continue
            if url.lower().startswith("tg://") or "t.me" in url.lower():
                continue
            hosts_total += 1
            hosts_unsafe += 1
            signals.append(
                Signal(
                    kind=SignalKind.HIDDEN_LINK,
                    weight=self._config.weight(SignalKind.HIDDEN_LINK),
                    evidence=url[:120],
                )
            )

        only_safe = hosts_total > 0 and hosts_unsafe == 0
        return signals, only_safe

    def _context_signals(
        self, context: MessageContext, normalized: NormalizedText
    ) -> list[Signal]:
        """Призывы к действию, рефералки, упоминания, контакты."""
        signals: list[Signal] = []

        cta = normalized.search(CALL_TO_ACTION_RE)
        if cta is not None:
            signals.append(
                Signal(
                    kind=SignalKind.CALL_TO_ACTION,
                    weight=self._config.weight(SignalKind.CALL_TO_ACTION),
                    evidence=cta.group(0),
                )
            )

        referral = normalized.search(REFERRAL_RE)
        if referral is not None:
            signals.append(
                Signal(
                    kind=SignalKind.REFERRAL,
                    weight=self._config.weight(SignalKind.REFERRAL),
                    evidence=referral.group(0),
                )
            )

        sales = normalized.search(SALES_RE)
        if sales is not None:
            signals.append(
                Signal(
                    kind=SignalKind.SALES_PITCH,
                    weight=self._config.weight(SignalKind.SALES_PITCH),
                    evidence=sales.group(0),
                )
            )

        mentions = self._collect_mentions(context)
        # Бот важнее обычного аккаунта: если среди упоминаний есть бот, фиксируем
        # именно его. Сигнал один на сообщение — пять ботов в тексте не дают
        # пятикратный вес, иначе одно правило перевесило бы весь скор.
        bots = [username for username in mentions if _is_bot_username(username)]
        if bots:
            signals.append(
                Signal(
                    kind=SignalKind.BOT_MENTION,
                    weight=self._config.weight(SignalKind.BOT_MENTION),
                    evidence=f"@{bots[0]}",
                )
            )
        elif mentions:
            signals.append(
                Signal(
                    kind=SignalKind.MENTION,
                    weight=self._config.weight(SignalKind.MENTION),
                    evidence=f"@{mentions[0]}",
                )
            )

        if self._has_contact(context):
            signals.append(
                Signal(
                    kind=SignalKind.CONTACT,
                    weight=self._config.weight(SignalKind.CONTACT),
                    evidence="contact_details",
                )
            )
        return signals

    def _collect_mentions(self, context: MessageContext) -> list[str]:
        """Упоминания посторонних аккаунтов (без доверенных)."""
        found = {match.group(1).lower() for match in _MENTION_RE.finditer(context.payload)}
        found.update(name.lstrip("@").lower() for name in context.mentions)
        return sorted(found - context.known_safe_usernames)

    @staticmethod
    def _has_contact(context: MessageContext) -> bool:
        """Телефон или мессенджер в сообщении."""
        if _MESSENGER_RE.search(context.text):
            return True
        return bool(_PHONE_RE.search(context.text))

    def _behaviour_signals(
        self,
        context: MessageContext,
        normalized: NormalizedText,
        has_category: bool,
        keyword_obfuscated: bool = False,
    ) -> list[Signal]:
        """Поведенческие признаки: обфускация, пересылка, новичок в чате."""
        signals: list[Signal] = []
        if (normalized.obfuscated or keyword_obfuscated) and has_category:
            signals.append(
                Signal(
                    kind=SignalKind.OBFUSCATION,
                    weight=self._config.weight(SignalKind.OBFUSCATION),
                    evidence="obfuscated_text",
                )
            )
        if context.forward_from_chat:
            signals.append(
                Signal(
                    kind=SignalKind.FORWARD,
                    weight=self._config.weight(SignalKind.FORWARD),
                    evidence="forward_from_chat",
                )
            )
        if context.duplicate_authors >= self._config.duplicate_threshold:
            # Чем больше аккаунтов повторили текст, тем меньше шансов на совпадение:
            # три автора — подозрительно, пять — уже рассылка. Надбавку ограничиваем,
            # чтобы одно правило не перевешивало весь скор.
            over = context.duplicate_authors - self._config.duplicate_threshold + 1
            signals.append(
                Signal(
                    kind=SignalKind.DUPLICATE,
                    weight=self._config.weight(SignalKind.DUPLICATE) * min(over, 3),
                    evidence=f"{context.duplicate_authors} авторов одного текста",
                )
            )
        if context.external_ban:
            signals.append(
                Signal(
                    kind=SignalKind.EXTERNAL_BAN,
                    weight=self._config.weight(SignalKind.EXTERNAL_BAN),
                    evidence=f"blocklist:{context.external_ban_source or 'unknown'}",
                )
            )
        if context.is_new_member and has_category:
            signals.append(
                Signal(
                    kind=SignalKind.NEW_MEMBER,
                    weight=self._config.weight(SignalKind.NEW_MEMBER),
                    evidence="new_member",
                )
            )
        return signals

    # ------------------------------------------------------------------ #
    #                        АНТИ-FP И РЕШЕНИЕ                            #
    # ------------------------------------------------------------------ #

    #: Факторы, которые не оправдываются «поиском книги».
    #: Рассылка здесь же: один и тот же текст с нескольких аккаунтов не
    #: становится книжным запросом от того, что в нём есть слово «ищу».
    AGGRESSIVE_FACTORS: Final[frozenset[SignalKind]] = frozenset(
        {
            SignalKind.TELEGRAM_INVITE,
            SignalKind.HIDDEN_LINK,
            SignalKind.REFERRAL,
            SignalKind.CALL_TO_ACTION,
            SignalKind.DUPLICATE,
            # «Ищу книгу, пиши в @promo_bot» — книжные слова не делают бота
            # книжным. Доверенные боты (Флибуста) сюда не попадают вовсе:
            # они в `known_safe_usernames`.
            SignalKind.BOT_MENTION,
        }
    )

    @staticmethod
    def _detect_book_intent(normalized: NormalizedText, signals: Sequence[Signal]) -> bool:
        """Похоже ли сообщение на поисковый/книжный запрос."""
        if normalized.search(BOOK_INTENT_RE) is not None:
            return True
        # Короткая фраза без единого спам-фактора — почти наверняка название книги.
        has_hard = any(signal.is_hard for signal in signals)
        return not has_hard and 0 < normalized.word_count <= 6

    @classmethod
    def _has_aggressive_factor(cls, hard: Sequence[Signal]) -> bool:
        return any(signal.kind in cls.AGGRESSIVE_FACTORS for signal in hard)

    @staticmethod
    def _score(signals: Iterable[Signal]) -> float:
        return round(sum(signal.weight for signal in signals), 3)

    def _decide(
        self, score: float, categories: frozenset[Category], hard: Sequence[Signal]
    ) -> tuple[Action, VerdictReason]:
        """Переводит скор в санкцию согласно порогам."""
        if hard and categories & self._config.escalate_categories:
            return Action.DELETE_AND_BAN, VerdictReason.PROHIBITED_CATEGORY
        if score >= self._config.ban_threshold:
            return Action.DELETE_AND_BAN, VerdictReason.SPAM_DETECTED
        if score >= self._config.mute_threshold:
            return Action.DELETE_AND_MUTE, VerdictReason.SPAM_DETECTED
        if score >= self._config.delete_threshold:
            return Action.DELETE, VerdictReason.SPAM_DETECTED
        return Action.ALLOW, VerdictReason.BELOW_THRESHOLD
