"""
Бот — отвечает на кнопки. Долгоживущий процесс.

Это НЕ опрос каналов: polling здесь — это aiogram, слушающий нажатия кнопок в
Telegram. Каналы через Telethon опрашивает только scripts/daily_fetch.py по
крону, раз в сутки — таково проектное ограничение по риску бана техаккаунта.

Ни одна кнопка не запускает конвейер: показ всегда идёт из БД, мгновенно.

Запуск: .venv/bin/python scripts/run_bot.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Dispatcher  # noqa: E402

from src.delivery import make_bot, setup_commands  # noqa: E402
from src.delivery.bot_ui import router  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    bot = make_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    # меню команд ставим из кода, а не через @BotFather
    await setup_commands(bot)

    me = await bot.get_me()
    print(f"бот @{me.username} запущен, слушаю кнопки. Ctrl+C — стоп")
    print("в Telegram: открой чат с ботом и отправь /start")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
