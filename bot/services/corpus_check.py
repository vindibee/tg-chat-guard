"""Регрессионный прогон размеченной выборки через движок.

Замыкает цикл обучения: админ помечает сообщения командами `/spam` и `/ham`,
а этот прогон показывает, что изменилось после правки словаря — сколько
«хороших» текстов фильтр начал ловить и сколько спама стал пропускать.

Прогон намеренно использует **только текст**: ни блок-листов, ни счётчика
дубликатов, ни профиля. Так измеряется именно качество правил, а результат
воспроизводим — два запуска подряд дают одно и то же.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bot.services.antispam_engine import AntiSpamEngine, MessageContext
from bot.services.sample_service import HAM, SPAM, SampleService

logger = logging.getLogger(__name__)

__all__ = ["CorpusEvaluator", "CorpusReport", "SampleOutcome"]


@dataclass(frozen=True, slots=True)
class SampleOutcome:
    """Результат проверки одного примера."""

    sample_id: int
    label: str
    text: str
    score: float
    action: str
    reason: str
    categories: str

    @property
    def excerpt(self) -> str:
        text = " ".join(self.text.split())
        return text if len(text) <= 90 else f"{text[:87]}..."


@dataclass(frozen=True, slots=True)
class CorpusReport:
    """Сводка прогона."""

    spam_total: int
    ham_total: int
    spam_caught: int
    ham_clean: int
    missed: tuple[SampleOutcome, ...]
    false_positives: tuple[SampleOutcome, ...]
    elapsed_ms: float

    @property
    def is_empty(self) -> bool:
        return not (self.spam_total or self.ham_total)

    @property
    def spam_rate(self) -> float:
        """Доля пойманного спама, %."""
        return 100.0 * self.spam_caught / self.spam_total if self.spam_total else 0.0

    @property
    def false_positive_rate(self) -> float:
        """Доля «хороших» текстов, которые фильтр счёл спамом, %."""
        if not self.ham_total:
            return 0.0
        return 100.0 * (self.ham_total - self.ham_clean) / self.ham_total

    @property
    def is_clean(self) -> bool:
        """Ни одного ложного срабатывания и весь спам пойман."""
        return not self.missed and not self.false_positives


class CorpusEvaluator:
    """Прогоняет выборку через движок и считает промахи."""

    def __init__(self, samples: SampleService, engine: AntiSpamEngine) -> None:
        self._samples = samples
        self._engine = engine

    async def run(self, *, limit: int = 2000, examples: int = 5) -> CorpusReport:
        """Прогоняет всю выборку.

        Args:
            limit: максимум примеров (защита от разросшегося корпуса).
            examples: сколько промахов каждого вида вернуть для показа.
        """
        corpus = await self._samples.all_samples(limit=limit)
        started = time.perf_counter()

        spam_total = ham_total = spam_caught = ham_clean = 0
        missed: list[SampleOutcome] = []
        false_positives: list[SampleOutcome] = []

        for sample in corpus:
            verdict = self._engine.evaluate(MessageContext(text=sample.text))
            outcome = SampleOutcome(
                sample_id=sample.id,
                label=sample.label,
                text=sample.text,
                score=verdict.score,
                action=verdict.action.value,
                reason=verdict.reason.value,
                categories=",".join(sorted(c.value for c in verdict.categories)),
            )
            if sample.label == SPAM:
                spam_total += 1
                if verdict.is_spam:
                    spam_caught += 1
                else:
                    missed.append(outcome)
            elif sample.label == HAM:
                ham_total += 1
                if verdict.is_spam:
                    false_positives.append(outcome)
                else:
                    ham_clean += 1

        elapsed_ms = (time.perf_counter() - started) * 1000
        report = CorpusReport(
            spam_total=spam_total,
            ham_total=ham_total,
            spam_caught=spam_caught,
            ham_clean=ham_clean,
            # Ложные срабатывания показываем от худшего: у них выше скор.
            missed=tuple(sorted(missed, key=lambda item: item.score)[:examples]),
            false_positives=tuple(
                sorted(false_positives, key=lambda item: -item.score)[:examples]
            ),
            elapsed_ms=elapsed_ms,
        )
        logger.info(
            "Регрессионный прогон выборки",
            extra={
                "component": "corpus",
                "spam_total": spam_total,
                "ham_total": ham_total,
                "spam_caught": spam_caught,
                "false_positives": ham_total - ham_clean,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )
        return report
