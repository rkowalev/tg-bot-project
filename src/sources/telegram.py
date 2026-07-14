"""
Источник — Telegram-канал через Telethon (User API).

Переиспользует ту же сессию и креды, что и разведка Итерации 0
(scripts/explore_channel.py): файл explore.session в корне, .env с
API_ID/API_HASH/CHANNEL. На этой итерации отдаёт только (текст, дата) —
модель Vacancy собирает parsing/, источник её не знает.
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
CHANNEL = os.environ["CHANNEL"]

SESSION_NAME = "explore"


async def iter_posts(limit: int) -> AsyncIterator[tuple[str, datetime]]:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    try:
        async for message in client.iter_messages(CHANNEL, limit=limit):
            yield message.text or "", message.date
    finally:
        await client.disconnect()
