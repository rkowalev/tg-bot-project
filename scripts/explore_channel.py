"""
Итерация 0 — разведка Telegram-канала.

Разовый скрипт: читает последние 20 постов канала через Telethon (User API),
чтобы своими глазами увидеть СЫРОЙ текст постов, из которого дальше будем
извлекать структуру. В конвейер (src/) НЕ входит.

Первый запуск интерактивный — Telethon спросит номер телефона и код из
Telegram, после чего создастся explore.session и код больше не спрашивается.

Запуск: .venv/bin/python scripts/explore_channel.py
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
CHANNEL = os.environ["CHANNEL"]

SESSION_NAME = "explore"  # даст файл explore.session в корне проекта


async def main() -> None:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # тут спросит телефон/код при первом запуске

    async for message in client.iter_messages(CHANNEL, limit=20):
        print(f"{message.date}")
        print("-" * 40)
        print(message.text if message.text else "[пост без текста]")
        print("-" * 40)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
