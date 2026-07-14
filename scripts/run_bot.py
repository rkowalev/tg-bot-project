"""
Точка входа: один запуск = один проход конвейера.

Вечного polling-цикла НЕТ намеренно: частый опрос каналов через Telethon —
риск бана техаккаунта. Запускать по cron или руками.

  LIMIT=50 .venv/bin/python scripts/run_bot.py     # сколько постов с канала
  DRY_RUN=1 .venv/bin/python scripts/run_bot.py    # без отправки в Telegram

Пример cron (раз в 2 часа):
  0 */2 * * * cd /path/to/tg-bot-project && .venv/bin/python scripts/run_bot.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.criteria import RUSLAN  # noqa: E402
from src.delivery import make_bot  # noqa: E402
from src.enrichment.enricher import STATS as CACHE  # noqa: E402
from src.pipeline import run_once  # noqa: E402

LIMIT = int(os.environ.get("LIMIT", "50"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


async def main() -> None:
    bot = None if DRY_RUN else make_bot()
    started = time.perf_counter()

    print(f"Критерии: {RUSLAN.model_dump()}")
    print(f"Постов с канала: {LIMIT}{'  [DRY_RUN — без отправки]' if DRY_RUN else ''}\n")

    try:
        stats = await run_once(RUSLAN, bot, LIMIT)
    finally:
        if bot is not None:
            await bot.session.close()

    elapsed = time.perf_counter() - started
    print("--- воронка ---")
    print(stats.report())
    print("\n--- цена и время ---")
    print(f"время прогона:           {elapsed:.1f} с")
    if CACHE.calls:
        print(f"входных токенов без кэша:{CACHE.input_without_cache:8d}")
        print(f"с кэшем по цене вышло:   {CACHE.effective_input:8d}")
        saved = CACHE.input_without_cache - CACHE.effective_input
        print(f"экономия на кэше:        {saved / CACHE.input_without_cache * 100:.0f}%")


if __name__ == "__main__":
    asyncio.run(main())
