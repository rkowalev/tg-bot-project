"""
Итерация 3 — прогон полного конвейера с фильтром.

источник -> парсинг -> обогащение -> фильтр. Печатает только прошедшие
вакансии со score и reasoning. В конце — воронка: сколько постов, сколько
прошло правила, сколько получило какой score, и сколько вызовов ИИ сэкономили.

LIMIT=15 по умолчанию. Замер: LIMIT=300 .venv/bin/python scripts/filter_channel.py

Запуск: .venv/bin/python scripts/filter_channel.py
"""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.criteria import RUSLAN  # noqa: E402
from src.enrichment import enrich_vacancy  # noqa: E402
from src.filters import Score, filter_vacancy  # noqa: E402
from src.filters import relevance as relevance_module  # noqa: E402
from src.parsing import parse_vacancy  # noqa: E402
from src.sources.telegram import iter_posts  # noqa: E402

LIMIT = int(os.environ.get("LIMIT", "15"))


async def main() -> None:
    posts = 0
    passed_rules = 0
    scores: Counter[str] = Counter()
    shown = 0

    print(f"Критерии: {RUSLAN.model_dump()}")
    print(f"Постов: {LIMIT}\n")

    async for raw_text, posted_at in iter_posts(LIMIT):
        posts += 1
        vacancy = enrich_vacancy(parse_vacancy(raw_text, posted_at))
        result = filter_vacancy(vacancy, RUSLAN)

        # score is not None -> правила прошла и ИИ ответил
        if result.score is not None or not result.reasons:
            passed_rules += 1
        if result.score is not None:
            scores[result.score.value] += 1

        if not result.passed:
            continue

        shown += 1
        print("=" * 60)
        print(f"{vacancy.title or '(должность не распознана)'}")
        print(f"  компания: {vacancy.company or '—'}")
        print(f"  грейд:    {vacancy.grade.value if vacancy.grade else '—'}")
        print(f"  формат:   {vacancy.work_format.value if vacancy.work_format else '—'}")
        print(f"  зарплата: {_salary(vacancy)}")
        print(f"  стек:     {', '.join(vacancy.stack) or '—'}")
        print(f"  контакт:  {vacancy.contact or '—'}")
        print(f"  SCORE:    {result.score.value.upper() if result.score else '?'}")
        print(f"  почему:   {result.reasoning}")

    _print_funnel(posts, passed_rules, scores, shown)


def _salary(vacancy) -> str:
    if not vacancy.salary or vacancy.salary.min_value is None:
        return "не указана"
    gross = {True: "гросс", False: "на руки", None: "?"}[vacancy.salary.gross]
    low = vacancy.salary.min_value // 1000
    high = (vacancy.salary.max_value or vacancy.salary.min_value) // 1000
    span = f"{low}к" if low == high else f"{low}-{high}к"
    return f"{span} {gross}"


def _print_funnel(posts: int, passed_rules: int, scores: Counter, shown: int) -> None:
    print("\n" + "=" * 60)
    print("--- воронка ---")
    print(f"{posts:4d} (100.0%)  постов прочитано")
    print(f"{passed_rules:4d} ({passed_rules / posts * 100:5.1f}%)  прошло жёсткие правила")
    for score in ("high", "medium", "low"):
        n = scores[score]
        print(f"{n:4d} ({n / posts * 100:5.1f}%)    из них score={score}")
    print(f"{shown:4d} ({shown / posts * 100:5.1f}%)  показано (high + medium)")

    saved = posts - relevance_module.STATS.calls
    print("\n--- цена ---")
    print(f"вызовов ИИ-оценки:     {relevance_module.STATS.calls}")
    print(f"сэкономлено вызовов:   {saved} (правила отсекли до ИИ)")
    if relevance_module.STATS.failures:
        print(f"оценок не удалось:     {relevance_module.STATS.failures}")


if __name__ == "__main__":
    asyncio.run(main())
