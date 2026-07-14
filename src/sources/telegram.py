"""
Источник — Telegram-каналы через Telethon (User API).

ВНИМАНИЕ, два разных механизма Telegram в проекте, не путать:
  - ЧТЕНИЕ каналов (здесь) — Telethon под техаккаунтом, api_id/api_hash + сессия;
  - ОТПРАВКА сводок (delivery/) — обычный бот aiogram под BOT_TOKEN от @BotFather.
Разные авторизации, разные креды.

Отдаёт RawPost, а не голый текст: для дедупликации и ссылки на пост нужны
channel и message_id. Модель Vacancy источник по-прежнему не знает — её
собирает parsing/.
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime

from dotenv import load_dotenv
from pydantic import BaseModel
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

# CHANNEL — исторически один канал; CHANNELS — список через запятую.
# Поддерживаем оба, чтобы старые скрипты не сломались.
CHANNELS = [
    name.strip()
    for name in os.environ.get("CHANNELS", os.environ.get("CHANNEL", "")).split(",")
    if name.strip()
]

SESSION_NAME = "explore"


class RawPost(BaseModel):
    channel: str
    message_id: int
    text: str
    posted_at: datetime

    @property
    def link(self) -> str:
        return f"https://t.me/{self.channel.lstrip('@')}/{self.message_id}"


async def iter_posts(
    limit: int, channels: list[str] | None = None
) -> AsyncIterator[RawPost]:
    """limit — сколько последних постов брать С КАЖДОГО канала."""
    targets = channels or CHANNELS
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    try:
        for channel in targets:
            async for message in client.iter_messages(channel, limit=limit):
                yield RawPost(
                    channel=channel,
                    message_id=message.id,
                    text=message.text or "",
                    posted_at=message.date,
                )
    finally:
        await client.disconnect()
