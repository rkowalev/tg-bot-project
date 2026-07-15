"""
Переоценка архива под ТЕКУЩИЕ критерии. Разовый скрипт, не часть конвейера.

Зачем. Смена критериев не трогает уже оценённые записи: в архиве соседствуют
вакансии, отобранные по разным правилам. Замерено 2026-07-15 — Kotlin за
160-210к и C# за 270к висели как medium с прогона, где порога 230к и жёсткого
Python ещё не было. Выглядит как баг фильтра, хотя правила ни при чём.

Исходники берём из БД (vacancies.raw_text) — сети не нужно. В Telegram лезем
только за записями, сделанными ДО появления этой колонки: у них raw_text=NULL.
Это важно после переезда на VPS: .session живёт в одном месте, а БД можно
скачать и разбираться офлайн.

Берём ТОЛЬКО протухшие (criteria_hash != текущего): переоценивать свежие
означало бы платить ИИ за уже известный ответ.

Отпечаток следит за КРИТЕРИЯМИ, но не за промптом. Правка шкалы в
relevance.py критерии не меняет — такие записи протухшими не считаются, и
перетряхнуть их можно только руками, флагом --all. Вшивать версию промпта в
отпечаток не стали: в разработке он правится часто, и каждая мелкая правка
формулировки заставляла бы платить за переоценку всего архива.

По умолчанию НИЧЕГО не пишет — показывает, что изменится. Писать: --apply
    .venv/bin/python scripts/reassess.py           # протухшие, просмотр
    .venv/bin/python scripts/reassess.py --apply   # протухшие, записать
    .venv/bin/python scripts/reassess.py --all --apply   # ВСЕ (правка промпта)
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient  # noqa: E402

from src.delivery.telegram_bot import format_message  # noqa: E402
from src.enrichment import enrich_vacancy  # noqa: E402
from src.filters import filter_vacancy  # noqa: E402
from src.parsing import parse_vacancy  # noqa: E402
from src.sources.telegram import API_ID, API_HASH, SESSION_NAME  # noqa: E402
from src.storage import (  # noqa: E402
    connect,
    criteria_fingerprint,
    drop_vacancy,
    get_criteria,
    reassess_vacancy,
    stale_vacancies,
)

APPLY = "--apply" in sys.argv
ALL = "--all" in sys.argv


async def _fetch_posts(rows) -> dict[str, str]:
    """
    Исходные тексты по content_hash.

    Сначала из БД. В Telegram идём только за теми, у кого raw_text=NULL —
    это записи до появления колонки. Когда они кончатся, скрипт станет
    полностью офлайновым.
    """
    texts = {r["content_hash"]: r["raw_text"] for r in rows if r["raw_text"]}
    missing = [r for r in rows if not r["raw_text"]]
    if not missing:
        print(f"исходники: все {len(texts)} из БД, сеть не нужна")
        return texts

    print(f"исходники: {len(texts)} из БД, {len(missing)} тяну из Telegram (старые записи)")
    by_channel: dict[str, list] = {}
    for row in missing:
        by_channel.setdefault(row["channel"], []).append(row)

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()
    try:
        for channel, channel_rows in by_channel.items():
            ids = [r["message_id"] for r in channel_rows]
            messages = await client.get_messages(channel, ids=ids)
            for row, message in zip(channel_rows, messages):
                if message is None or not message.text:
                    continue  # пост удалён из канала — трогать запись не будем
                texts[row["content_hash"]] = message.text
    finally:
        await client.disconnect()
    return texts


async def main() -> None:
    conn = connect()
    try:
        criteria = get_criteria(conn)
        if criteria is None:
            print("критериев нет — сначала онбординг в боте (/start)")
            return

        fingerprint = criteria_fingerprint(criteria)
        if ALL:
            rows = conn.execute("SELECT * FROM vacancies ORDER BY posted_at").fetchall()
            print(f"--all: переоцениваю ВСЕ записи ({len(rows)})")
        else:
            rows = stale_vacancies(conn, fingerprint)
        if not rows:
            print("протухших записей нет — архив уже под текущими критериями")
            return

        print(f"записей к переоценке: {len(rows)}; тяну исходники из Telegram…")
        texts = await _fetch_posts(rows)
        print(f"достал постов: {len(texts)} из {len(rows)}\n")

        kept, dropped, missing = [], [], []
        for row in rows:
            text = texts.get(row["content_hash"])
            if text is None:
                missing.append(row["title"] or "?")
                continue

            # posted_at в БД — строка, парсеру нужен datetime
            vacancy = parse_vacancy(text, datetime.fromisoformat(row["posted_at"]))
            vacancy = await enrich_vacancy(vacancy)
            result = await filter_vacancy(vacancy, criteria)

            was = row["score"] or "—"
            now = result.score.value if result.score else "—"
            title = (row["title"] or "?")[:44]

            if result.passed:
                kept.append((row, vacancy, result))
                mark = "=" if was == now else ">"
                print(f"  {mark} {was:7} -> {now:8} {title}")
            else:
                dropped.append(row)
                why = "; ".join(result.reasons) or now
                print(f"  ✗ {was:7} -> УБРАТЬ   {title}\n      причина: {why}")

        print(f"\nостаётся: {len(kept)}, убрать: {len(dropped)}")
        if missing:
            print(f"пост удалён из канала, не трогаю: {len(missing)} ({missing})")

        if not APPLY:
            print("\nэто просмотр, база не тронута. Записать: --apply")
            return

        for row, vacancy, result in kept:
            reassess_vacancy(
                conn,
                hash_value=row["content_hash"],
                vacancy=vacancy,
                score=result.score.value if result.score else None,
                reasoning=result.reasoning,
                message=format_message(vacancy, result, row["link"]),
                criteria_hash=fingerprint,
            )
        for row in dropped:
            drop_vacancy(conn, row["content_hash"])
        conn.commit()
        print(f"\nзаписано: обновлено {len(kept)}, удалено {len(dropped)}")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
