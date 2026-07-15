"""
Источник — Telegram-каналы через Telethon (User API).

ВНИМАНИЕ, два разных механизма Telegram в проекте, не путать:
  - ЧТЕНИЕ каналов (здесь) — Telethon под техаккаунтом, api_id/api_hash + сессия;
  - ОТПРАВКА сводок (delivery/) — обычный бот aiogram под BOT_TOKEN от @BotFather.
Разные авторизации, разные креды.

Отдаёт RawPost, а не голый текст: для дедупликации и ссылки на пост нужны
channel и message_id. Модель Vacancy источник по-прежнему не знает — её
собирает parsing/.

Откуда берутся каналы: если задан CHANNELS/CHANNEL — из него; иначе из
ПОДПИСОК техаккаунта. Аккаунт технический, посторонних подписок на нём нет,
поэтому «на что подписан» = «что читаем»: подписался в приложении — источник
появился, .env править не надо.
"""

import os
from collections.abc import AsyncIterator
from datetime import datetime

from dotenv import load_dotenv
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.types import Channel

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]


def normalize_channel(name: str) -> str:
    """
    Имя канала — это ещё и ключ дедупликации в seen_posts, поэтому форма записи
    обязана быть одна. В БД лежит '@qa_jobs', а Telethon отдаёт username без
    собаки — без нормализации ключ разъедется и вся история канала поедет в ИИ
    по второму разу.
    """
    name = name.strip()
    return name if name.startswith("@") else f"@{name}"


# CHANNEL — исторически один канал; CHANNELS — список через запятую.
# Поддерживаем оба, чтобы старые скрипты не сломались.
CHANNELS = [
    normalize_channel(name)
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


async def subscribed_channels(client) -> list[str]:
    """
    Подписки техаккаунта как список источников.

    Берём и broadcast-каналы, и мегагруппы. Это не перестраховка: @qa_jobs —
    мегагруппа (broadcast=False), и фильтр «только broadcast» отсёк бы
    единственный рабочий источник, причём молча. В терминах Telegram и то и
    другое — Channel; обычные группы и переписки с людьми имеют другой тип и
    сюда не попадают.
    """
    found: list[str] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, Channel):
            continue
        if not entity.username:
            # Приватный канал: без username не собрать ни ссылку на пост, ни
            # стабильный ключ дедупликации. Громко говорим, а не глотаем.
            print(f"Пропускаю {dialog.name!r}: приватный канал без username")
            continue
        found.append(normalize_channel(entity.username))
    return found


async def iter_posts(
    limit: int, channels: list[str] | None = None
) -> AsyncIterator[RawPost]:
    """limit — сколько последних постов брать С КАЖДОГО канала."""
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    try:
        # Явный список важнее подписок: аргумент -> .env -> подписки.
        targets = channels or CHANNELS or await subscribed_channels(client)
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
