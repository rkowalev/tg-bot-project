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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "daily_fetch.log"


class _Tee:
    """
    Отчёт идёт и в stdout, и в файл.

    Под кроном stdout уходит в никуда: не придёт дайджест — и причину узнать
    неоткуда. Пишем сами, а не надеемся на редирект в crontab, который легко
    забыть. flush после каждой строки: иначе при падении посреди прогона лог
    останется пустым, а это ровно тот случай, когда он и нужен.
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

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
        unseen = count_unseen(conn)
        # Сколько нашли — вместе с отметкой времени: ноль тоже ответ, он и
        # отличает «сходил вхолостую» от «не запускался».
        touch_last_fetch(conn, unseen - before)
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
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        sys.stdout = _Tee(sys.__stdout__, log_file)
        print(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====")
        try:
            asyncio.run(main())
        except Exception:
            # Падение обязано остаться в логе: под кроном traceback в stderr
            # не увидит никто, а молчащий крон неотличим от «новых нет».
            import traceback

            traceback.print_exc(file=sys.stdout)
            raise
        finally:
            # Вернуть stdout ОБЯЗАТЕЛЬНО: иначе он останется указывать на
            # файл, который закроет with, и интерпретатор упадёт на выходе,
            # пытаясь его сбросить. Работа при этом уже сделана — под кроном
            # это выглядело бы как падение на ровном месте.
            sys.stdout = sys.__stdout__
