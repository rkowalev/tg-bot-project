"""
Фоновый сбор — точка входа для крона. Отработал и вышел, не демон.

Что делает: проверяет паузу -> собирает -> кладёт в БД -> шлёт ОДИН тихий
дайджест «доступно N новых». Вакансии по одной НЕ шлёт: их пользователь
смотрит кнопкой, мгновенно из БД.

Новых нет — молчит. Пауза включена — выходит сразу, до Telethon и ИИ.

Крон (раз в сутки, вечером):
  0 20 * * * cd /path/to/tg-bot-project && .venv/bin/python scripts/daily_fetch.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.delivery import make_bot  # noqa: E402
from src.delivery.bot_ui import digest_keyboard  # noqa: E402
from src.delivery.telegram_bot import CHAT_ID  # noqa: E402
from src.pipeline import run_once  # noqa: E402
from src.storage import (  # noqa: E402
    connect,
    count_unseen,
    get_criteria,
    is_fetch_enabled,
    touch_last_fetch,
)

LIMIT = int(os.environ.get("LIMIT", "50"))


async def main() -> None:
    conn = connect()
    try:
        # Пауза проверяется ПЕРВОЙ: при выключенном сборе не должно уйти ни
        # одного внешнего вызова — ни в Telegram, ни в API.
        if not is_fetch_enabled(conn):
            print("сбор на паузе — выхожу, ничего не тратя")
            return

        criteria = get_criteria(conn)
        if criteria is None:
            print("критериев нет — сначала пройди онбординг в боте (/start)")
            return

        before = count_unseen(conn)
    finally:
        conn.close()

    # bot=None: pipeline только собирает и кладёт в БД. Рассылка по одной
    # вакансии — не наш режим, дайджест уходит ниже одним сообщением.
    stats = await run_once(criteria, bot=None, limit=LIMIT)

    conn = connect()
    try:
        touch_last_fetch(conn)
        unseen = count_unseen(conn)
    finally:
        conn.close()

    print(stats.report())
    new_count = unseen - before
    print(f"\nновых для показа: {new_count} (всего непоказанных: {unseen})")

    if new_count <= 0:
        print("новых нет — не пишу")
        return

    bot = make_bot()
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"Доступно {new_count} новых вакансий по твоим критериям.",
            reply_markup=digest_keyboard,
            disable_notification=True,  # тихо: без звука и вибрации
        )
        print("дайджест отправлен")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
