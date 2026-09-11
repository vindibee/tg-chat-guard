"""Нормализация текста: защита от обхода фильтра спецсимволами и гомоглифами.

Модуль сознательно не зависит ни от aiogram, ни от настроек — это чистая функция
над строкой, которую можно юнит-тестировать и переиспользовать где угодно.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = ["NormalizedText", "TextCleaner", "normalize"]

#: Невидимые символы, которыми спамеры разрывают слова.
_INVISIBLE: Final[str] = (
    "​‌‍⁠⁡⁢⁣"  # zero-width / invisible operators
    "﻿­͏᠎⁤⁪⁫⁬⁭⁮⁯"
)
_INVISIBLE_TABLE: Final[dict[int, None]] = dict.fromkeys(map(ord, _INVISIBLE), None)

#: Латиница -> кириллица (для «кaзино», где `a` — латинская).
_LAT_TO_CYR: Final[dict[str, str]] = {
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "i": "і", "3": "з",
}
#: Кириллица -> латиница (для «сasino», где `с` — кириллическая).
_CYR_TO_LAT: Final[dict[str, str]] = {
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k", "м": "m",
    "о": "o", "р": "p", "т": "t", "х": "x", "у": "y", "і": "i", "ѕ": "s",
}
#: Leet-подстановки: «казин0», «bit©oin», «зар@боток».
_DELEET: Final[dict[str, str]] = {
    "0": "о", "1": "і", "3": "е", "4": "а", "5": "ѕ", "6": "б", "7": "т",
    "8": "в", "9": "g", "@": "а", "$": "s", "!": "і", "|": "і", "(": "с",
    "©": "с", "€": "е", "₽": "р", "¥": "у", "§": "s", "*": "",
}

_TRANSLATE_LAT_TO_CYR: Final[dict[int, str]] = str.maketrans(_LAT_TO_CYR)
_TRANSLATE_CYR_TO_LAT: Final[dict[int, str]] = str.maketrans(_CYR_TO_LAT)
_TRANSLATE_DELEET: Final[dict[int, str]] = str.maketrans(_DELEET)

#: Комбинирующие диакритики — ими «пачкают» буквы, чтобы обойти фильтр.
_COMBINING_RE: Final[re.Pattern[str]] = re.compile(
    r"[̀-ͯ҃-҉֑-ֽ᪰-᫿᷀-᷿"
    r"⃐-⃰︠-︯]"
)
_LATIN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]")
_CYRILLIC_RE: Final[re.Pattern[str]] = re.compile(r"[а-яёіѕ]")
#: Символы leet-подстановки; если их нет, соответствующий вариант не строим.
_LEET_RE: Final[re.Pattern[str]] = re.compile(r"[0-9@$!|(©€₽¥§*]")
#: Кириллица и латиница внутри одного слова (цифры между ними допускаются).
_MIXED_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(r"[а-яё][0-9]*[a-z]|[a-z][0-9]*[а-яё]")

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_SEPARATORS_RE: Final[re.Pattern[str]] = re.compile(r"[^0-9a-zа-яёіѕ]+")
_REPEATS_RE: Final[re.Pattern[str]] = re.compile(r"(.)\1{2,}")
_SQUASH_REPEATS_RE: Final[re.Pattern[str]] = re.compile(r"(.)\1+")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-zа-яёіѕ]+")
#: Пунктуация внутри слова: «к.р.и.п.т.а», «з-а-р-а-б-о-т-о-к».
#: Пробел и `_` сюда НЕ входят: пробел склеил бы обычные соседние слова,
#: а подчёркивание легально в юзернеймах (@book_search_bot).
#: Оба символа всё равно удаляются в `squashed`-форме, так что обход
#: вида «к_а_з_и_н_о» ловится на уровне анти-обфускации.
_INWORD_PUNCT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[0-9a-zа-яёіѕ])[.\-*+~^'\"`/\|:,]{1,2}(?=[0-9a-zа-яёіѕ])"
)
#: Слово, разорванное пунктуацией минимум дважды: «к.а.з», «с-п-а».
#: Одиночный дефис («какой-то», «bonus-vulkan») под это не подпадает.
_SPLIT_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-zа-яёіѕ][.\-*+~^'\"`/\|:,][0-9a-zа-яёіѕ][.\-*+~^'\"`/\|:,][0-9a-zа-яёіѕ]"
)
#: Растянутые пробелами одиночные буквы: «к р и п т а» -> «крипта».
#: Нужно не меньше четырёх букв подряд: в русском полно однобуквенных слов,
#: и «а я с ним» — это обычная речь, а не обход фильтра.
_SPACED_LETTERS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9a-zа-яёіѕ])(?:[0-9a-zа-яёіѕ] ){3,}[0-9a-zа-яёіѕ](?![0-9a-zа-яёіѕ])"
)


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """Результат нормализации.

    Attributes:
        raw: исходный текст.
        lowered: нижний регистр, без невидимых символов, NFKC, схлопнутые пробелы.
        variants: варианты для поиска «по словам» (с границами слов).
        squashed: варианты без разделителей и с схлопнутыми повторами —
            ловят «к р и п т а» и «кккриптааа», но матчатся как подстрока.
        tokens: слова нормализованного текста.
        obfuscated: текст содержит явные признаки обхода фильтра.
    """

    raw: str
    lowered: str
    variants: tuple[str, ...]
    squashed: tuple[str, ...]
    tokens: tuple[str, ...]
    obfuscated: bool

    @property
    def is_empty(self) -> bool:
        return not self.lowered.strip()

    @property
    def word_count(self) -> int:
        return len(self.tokens)

    def search(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        """Ищет паттерн по «словесным» вариантам (строгий уровень)."""
        for variant in self.variants:
            match = pattern.search(variant)
            if match is not None:
                return match
        return None

    def search_squashed(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        """Ищет паттерн по склеенным вариантам (уровень анти-обфускации)."""
        for variant in self.squashed:
            match = pattern.search(variant)
            if match is not None:
                return match
        return None


class TextCleaner:
    """Нормализатор текста сообщений."""

    @staticmethod
    def strip_invisible(text: str) -> str:
        """Удаляет zero-width символы и комбинирующие диакритики (`з̷а̷р̷`)."""
        text = text.translate(_INVISIBLE_TABLE)
        decomposed = unicodedata.normalize("NFKD", text)
        # Посимвольная сборка дорога на длинных текстах, а диакритики в чате —
        # редкость: сначала дешёвая проверка регуляркой.
        if _COMBINING_RE.search(decomposed):
            decomposed = "".join(
                ch for ch in decomposed if not unicodedata.combining(ch)
            )
        return unicodedata.normalize("NFKC", decomposed)

    @classmethod
    def base_form(cls, text: str) -> str:
        """Базовая форма: без невидимок, нижний регистр, ё→е, один пробел."""
        cleaned = cls.strip_invisible(text).lower().replace("ё", "е")
        return _WHITESPACE_RE.sub(" ", cleaned).strip()

    @staticmethod
    def _squash(text: str) -> str:
        """Убирает все разделители и схлопывает повторы символов."""
        glued = _SEPARATORS_RE.sub("", text)
        return _SQUASH_REPEATS_RE.sub(r"\1", glued)

    @classmethod
    def fingerprint(cls, text: str) -> str:
        """Устойчивый отпечаток текста для поиска одинаковых сообщений.

        Считается по склеенной форме: регистр, пробелы, пунктуация, эмодзи и
        повторы символов отбрасываются. Поэтому «Привет!!! Ищу людей 🙂» и
        «привет, ищу людеи» дают один отпечаток, а рассылка, размноженная
        случайными эмодзи, не обходит проверку.
        """
        squashed = cls._squash(cls.base_form(text))
        return hashlib.sha1(squashed.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def normalize(cls, text: str | None) -> NormalizedText:
        """Строит все формы текста, нужные движку антиспама."""
        raw = text or ""
        lowered = cls.base_form(raw)

        # Склейка разорванных слов: «к.р.и.п.т.а» / «к р и п т а» -> «крипта».
        joined = _INWORD_PUNCT_RE.sub("", lowered)
        joined, spaced_hits = _SPACED_LETTERS_RE.subn(
            lambda m: m.group(0).replace(" ", ""), joined
        )
        # «прииивееет» -> «прииивееет» с максимум двумя повторами.
        deduped = _REPEATS_RE.sub(r"\1\1", joined)
        deleet = deduped.translate(_TRANSLATE_DELEET) if _LEET_RE.search(deduped) else deduped

        has_latin = _LATIN_RE.search(lowered) is not None
        has_cyrillic = _CYRILLIC_RE.search(lowered) is not None

        variant_source: list[str] = [lowered, joined, deduped]
        if deleet != deduped:
            variant_source.append(deleet)

        variants: list[str] = []
        for source in variant_source:
            variants.append(source)
            # Латиница -> кириллица нужна, только если латиница вообще есть.
            if has_latin:
                variants.append(source.translate(_TRANSLATE_LAT_TO_CYR))
            if has_cyrillic:
                variants.append(source.translate(_TRANSLATE_CYR_TO_LAT))

        squashed = [cls._squash(variant) for variant in variants]

        # Одиночный дефис — это «какой-то» и «bonus-vulkan», а не обход фильтра.
        # Признаком обхода считаем невидимые символы, растянутые пробелами буквы,
        # смешение алфавитов или слово, разорванное пунктуацией дважды и более.
        obfuscated = (
            any(ch in raw for ch in _INVISIBLE)
            or spaced_hits > 0
            or _SPLIT_WORD_RE.search(lowered) is not None
            or cls._has_mixed_script_word(lowered)
        )

        return NormalizedText(
            raw=raw,
            lowered=lowered,
            variants=cls._dedup(variants),
            squashed=cls._dedup(squashed),
            tokens=tuple(_TOKEN_RE.findall(lowered)),
            obfuscated=obfuscated,
        )

    @staticmethod
    def _dedup(values: Iterable[str]) -> tuple[str, ...]:
        """Удаляет дубликаты, сохраняя порядок (важно для стабильности матчей)."""
        seen: dict[str, None] = {}
        for value in values:
            if value:
                seen.setdefault(value, None)
        return tuple(seen)

    @staticmethod
    def _has_mixed_script_word(text: str) -> bool:
        """True, если в одном слове смешаны кириллица и латиница (гомоглиф-атака).

        Быстрый отсев регуляркой: смешанное слово обязательно содержит стык
        двух алфавитов. Длину слова проверяем только для найденных кандидатов —
        короткие «ok», «100 zł» и прочее ложных срабатываний не дают.
        """
        if _MIXED_SCRIPT_RE.search(text) is None:
            return False
        for token in _TOKEN_RE.findall(text):
            if len(token) >= 4 and _MIXED_SCRIPT_RE.search(token) is not None:
                return True
        return False


def normalize(text: str | None) -> NormalizedText:
    """Шорткат для `TextCleaner.normalize`."""
    return TextCleaner.normalize(text)
