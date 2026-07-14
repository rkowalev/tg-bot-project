"""
Итерация 1 — прогон парсера на живых постах канала.

Читает последние посты через тот же Telethon-источник, что и разведка
(Итерация 0: та же сессия explore.session), прогоняет каждый через
parse_vacancy и печатает результат: саму модель Vacancy и отдельно
parse_flags — чтобы видеть, что не взялось. В конце — частотная таблица
parse_flags по всему прогону: снимок "что регулярки не берут" до Итерации 2
(ИИ-слой), с которым потом сравнивать прогресс. В конвейер (src/pipeline)
НЕ входит, это разовый прогон для ручной проверки регулярок.

Запуск: .venv/bin/python scripts/parse_channel.py
"""

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parsing import parse_vacancy  # noqa: E402
from src.sources.telegram import iter_posts  # noqa: E402

LIMIT = 300


async def main() -> None:
    flag_counts: Counter[str] = Counter()
    posts_seen = 0

    async for post in iter_posts(LIMIT):
        vacancy = parse_vacancy(post.text, post.posted_at)
        posts_seen += 1
        # "additional_salary_fork: <текст>" у каждого поста свой — для частотной
        # таблицы считаем это одной категорией, а не кучей уникальных строк
        flag_counts.update(flag.split(":", 1)[0] for flag in vacancy.parse_flags)

        print("=" * 60)
        print(vacancy.model_dump_json(indent=2))
        print("--- parse_flags ---")
        print(vacancy.parse_flags or "(нет пометок)")

    print("\n" + "=" * 60)
    print(f"ИТОГО постов: {posts_seen}")
    print("--- частота parse_flags ---")
    if not flag_counts:
        print("(пусто — ни одной пометки за весь прогон)")
    else:
        for flag, count in flag_counts.most_common():
            share = count / posts_seen * 100
            print(f"{count:4d} ({share:5.1f}%)  {flag}")


if __name__ == "__main__":
    asyncio.run(main())
