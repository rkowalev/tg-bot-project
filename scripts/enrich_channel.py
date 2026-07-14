"""
Итерация 2 — прогон конвейера с ИИ-слоем на живых постах.

Читает посты -> парсит регулярками (Итерация 1) -> дозаполняет через Claude
(Итерация 2) -> печатает финальную модель. В конце: частота parse_flags плюс
заполняемость полей, чтобы сравнить с baseline из 300 постов.

LIMIT=15 по умолчанию — отладка промпта стоит копейки. Поднять до 300 для
замера: LIMIT=300 .venv/bin/python scripts/enrich_channel.py

Запуск: .venv/bin/python scripts/enrich_channel.py
"""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.enrichment import MODEL, enrich_vacancy  # noqa: E402
from src.enrichment.enricher import STATS  # noqa: E402
from src.models.vacancy import Grade  # noqa: E402
from src.parsing import parse_vacancy  # noqa: E402
from src.sources.telegram import iter_posts  # noqa: E402

LIMIT = int(os.environ.get("LIMIT", "15"))


def _is_filled(vacancy, field: str) -> bool:
    """Заполнено ли поле по-настоящему (UNKNOWN у грейда — это не заполнено)."""
    value = getattr(vacancy, field)
    if value is None:
        return False
    if field == "grade":
        return value is not Grade.UNKNOWN
    return True


async def main() -> None:
    flag_counts: Counter[str] = Counter()
    filled: Counter[str] = Counter()
    posts_seen = 0

    print(f"Модель: {MODEL} | постов: {LIMIT}\n")

    async for post in iter_posts(LIMIT):
        vacancy = await enrich_vacancy(parse_vacancy(post.text, post.posted_at))
        posts_seen += 1

        flag_counts.update(flag.split(":", 1)[0] for flag in vacancy.parse_flags)
        for field in ("title", "company", "contact", "work_format"):
            if _is_filled(vacancy, field):
                filled[field] += 1
        if _is_filled(vacancy, "grade"):
            filled["grade"] += 1
        if vacancy.is_vacancy is not None:
            filled["is_vacancy"] += 1
        if vacancy.is_vacancy is False:
            filled["is_vacancy=false (мусор)"] += 1
        # нормализованная зарплата = ИИ реально посчитал числа, а не только raw
        if vacancy.salary is not None and vacancy.salary.min_value is not None:
            filled["salary (нормализована)"] += 1

        print("=" * 60)
        print(vacancy.model_dump_json(indent=2))
        print("--- parse_flags ---")
        print(vacancy.parse_flags or "(нет пометок)")

    print("\n" + "=" * 60)
    print(f"ИТОГО постов: {posts_seen}")

    print("\n--- заполняемость полей ---")
    for field in (
        "is_vacancy",
        "title",
        "grade",
        "salary (нормализована)",
        "company",
        "work_format",
        "contact",
        "is_vacancy=false (мусор)",
    ):
        count = filled[field]
        print(f"{count:4d} ({count / posts_seen * 100:5.1f}%)  {field}")

    print("\n--- частота parse_flags ---")
    if not flag_counts:
        print("(пусто — ни одной пометки за весь прогон)")
    else:
        for flag, count in flag_counts.most_common():
            print(f"{count:4d} ({count / posts_seen * 100:5.1f}%)  {flag}")

    _print_cache_stats()


def _print_cache_stats() -> None:
    """Кэш эфемерный (TTL ~5 мин): внутри прогона работает, между прогонами протухает."""
    print("\n--- prompt caching ---")
    if STATS.calls == 0:
        print("(вызовов не было)")
        return

    print(f"вызовов к API:            {STATS.calls}")
    print(f"записано в кэш:           {STATS.cache_creation} токенов (цена 1.25x, один раз)")
    print(f"прочитано из кэша:        {STATS.cache_read} токенов (цена 0.1x)")
    print(f"мимо кэша (текст постов): {STATS.uncached_input} токенов (полная цена)")

    if STATS.cache_read == 0 and STATS.calls > 1:
        print("\nкэш НЕ сработал — префикс промпта короче порога модели (4096 у Haiku 4.5)")
        return

    saved = STATS.input_without_cache - STATS.effective_input
    print(f"\nбез кэша заплатили бы:    {STATS.input_without_cache} входных токенов")
    print(f"с кэшем вышло по цене:    {STATS.effective_input} входных токенов")
    print(f"экономия:                 {saved} токенов "
          f"({saved / STATS.input_without_cache * 100:.0f}%)")


if __name__ == "__main__":
    asyncio.run(main())
