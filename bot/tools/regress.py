"""Прогон размеченной выборки по текущим правилам — без запуска бота.

    python -m bot.tools.regress

Код возврата 1, если есть ложные срабатывания: так прогон можно поставить в CI
и не дать правке словаря незаметно начать ловить книжные запросы.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bot.config import get_settings
from bot.db.session import create_database
from bot.services.antispam_engine import AntiSpamEngine, EngineConfig
from bot.services.corpus_check import CorpusEvaluator, CorpusReport
from bot.services.sample_service import SampleService

__all__ = ["main", "render"]


def render(report: CorpusReport) -> str:
    """Текстовый отчёт для консоли."""
    if report.is_empty:
        return (
            "Выборка пуста.\n"
            "Пополните её командами /spam и /ham в ответ на сообщения в чате."
        )

    total = report.spam_total + report.ham_total
    lines = [
        f"Прогон выборки: {total} примеров за {report.elapsed_ms:.0f} мс",
        "-" * 72,
        f"Спам пойман:          {report.spam_caught}/{report.spam_total} "
        f"({report.spam_rate:.1f}%)",
        f"Ложные срабатывания:  {report.ham_total - report.ham_clean}/{report.ham_total} "
        f"({report.false_positive_rate:.1f}%)",
    ]

    if report.false_positives:
        lines += ["", "ЗРЯ ПОЙМАНЫ (правила ошибаются на хорошем тексте):"]
        for outcome in report.false_positives:
            categories = f" [{outcome.categories}]" if outcome.categories else ""
            lines.append(f"  {outcome.score:5.1f}{categories}  {outcome.excerpt}")

    if report.missed:
        lines += ["", "ПРОПУЩЕННЫЙ СПАМ:"]
        for outcome in report.missed:
            lines.append(f"  {outcome.score:5.1f} [{outcome.reason}]  {outcome.excerpt}")

    lines += ["", "Прогон использует только текст: без блок-листов, дубликатов и профиля."]
    return "\n".join(lines)


async def run(limit: int, examples: int) -> CorpusReport:
    """Читает выборку из БД и прогоняет её через движок."""
    settings = get_settings()
    database = create_database(settings.database_url)
    try:
        # На свежей установке таблиц ещё нет — создаём, чтобы вместо
        # простыни traceback получить понятное «выборка пуста».
        await database.create_all()
        evaluator = CorpusEvaluator(
            SampleService(database), AntiSpamEngine(EngineConfig.from_settings(settings))
        )
        return await evaluator.run(limit=limit, examples=examples)
    finally:
        await database.dispose()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2000, help="максимум примеров")
    parser.add_argument("--examples", type=int, default=10, help="сколько промахов показать")
    args = parser.parse_args()

    report = asyncio.run(run(args.limit, args.examples))
    print(render(report))
    # Пропущенный спам — это про полноту, ложные срабатывания — про доверие к боту.
    # Роняем прогон только на вторых.
    return 1 if report.false_positives else 0


if __name__ == "__main__":
    sys.exit(main())
