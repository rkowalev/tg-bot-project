"""
Оркестратор конвейера. Теперь модули связывает ОН, а не прогонной скрипт.

Порядок этапов — не косметика, а решение проблемы скорости и цены. Дешёвое
идёт раньше дорогого, и каждый этап уменьшает пачку для следующего:

  1. читаем посты                          сеть, дёшево
  2. дедуп по (канал, message_id)          БД, ~0 мс     <- ДО любого ИИ
  3. парсим регулярками                    ~1 мс/пост
  4. предфильтр по формату и стеку         ~0 мс         <- ДО любого ИИ
  5. обогащаем (ИИ)                        ~2 с/пост     ПАРАЛЛЕЛЬНО
  6. фильтр: правила + ИИ-оценка           ~2 с/пост     ПАРАЛЛЕЛЬНО
  7. дедуп по хэшу текста + доставка       БД + Telegram

Шаги 2 и 4 — то, ради чего всё: на повторном прогоне почти все посты
отсеиваются на шаге 2, не доходя до платных вызовов. Замерено: без этого
1000 постов = ~50 минут, с этим обычный прогон = секунды.

Параллельность на шагах 5-6 даёт 7.4x (замерено), семафор держит нагрузку в
рамках rate limit.
"""

import asyncio
from dataclasses import dataclass, field

from aiogram import Bot

from src.delivery.telegram_bot import send_vacancy
from src.enrichment import enrich_vacancy
from src.filters import Criteria, FilterResult, filter_vacancy, passes_prefilter
from src.models.vacancy import Vacancy
from src.parsing import parse_vacancy
from src.sources.telegram import RawPost, iter_posts
from src.storage import (
    connect,
    content_hash,
    exists,
    mark_delivered,
    mark_seen,
    save_vacancy,
    seen_message_ids,
)

# Сколько ИИ-вызовов держим в воздухе одновременно. 10 — замеренный компромисс:
# ускорение 7.4x, при этом не упираемся в rate limit на младшем тарифе.
CONCURRENCY = 10


@dataclass
class RunStats:
    read: int = 0
    already_seen: int = 0
    cut_by_prefilter: int = 0
    enriched: int = 0
    cut_by_rules: int = 0
    assessed: int = 0
    duplicates: int = 0
    delivered: int = 0
    scores: dict[str, int] = field(default_factory=dict)

    def report(self) -> str:
        lines = [
            f"прочитано постов:        {self.read}",
            f"  уже видели (дедуп):    {self.already_seen}  <- ИИ не звали",
            f"  отсёк предфильтр:      {self.cut_by_prefilter}  <- ИИ не звали",
            f"обогащено (вызов ИИ):    {self.enriched}",
            f"  отсекли правила:       {self.cut_by_rules}",
            f"оценено ИИ (вызов ИИ):   {self.assessed}",
        ]
        for score, count in sorted(self.scores.items()):
            lines.append(f"    score={score}: {count}")
        lines += [
            f"дубли по хэшу текста:    {self.duplicates}",
            f"ДОСТАВЛЕНО:              {self.delivered}",
            "",
            f"вызовов ИИ всего:        {self.enriched + self.assessed}",
            f"сэкономлено вызовов:     {(self.already_seen + self.cut_by_prefilter) * 2}",
        ]
        return "\n".join(lines)


async def _gather_limited(coros: list, limit: int) -> list:
    semaphore = asyncio.Semaphore(limit)

    async def guarded(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(guarded(c) for c in coros))


async def run_once(
    criteria: Criteria,
    bot: Bot | None,
    limit: int,
    db_path=None,
) -> RunStats:
    stats = RunStats()
    conn = connect(db_path)

    try:
        # --- 1-2: читаем и сразу отсеиваем виденное ---
        fresh: list[RawPost] = []
        seen_cache: dict[str, set[int]] = {}
        async for post in iter_posts(limit):
            stats.read += 1
            if post.channel not in seen_cache:
                seen_cache[post.channel] = seen_message_ids(conn, post.channel)
            if post.message_id in seen_cache[post.channel]:
                stats.already_seen += 1
                continue
            fresh.append(post)

        # Помечаем виденными ВСЁ, что прочитали, даже отсеянное: на следующем
        # прогоне такой пост не должен снова тратить ни времени, ни токенов.
        for post in fresh:
            mark_seen(conn, post.channel, post.message_id)
        conn.commit()

        # --- 3-4: парсим и отсеиваем на регулярочных данных, до ИИ ---
        candidates: list[tuple[RawPost, Vacancy]] = []
        for post in fresh:
            vacancy = parse_vacancy(post.text, post.posted_at)
            if not passes_prefilter(vacancy, criteria):
                stats.cut_by_prefilter += 1
                continue
            candidates.append((post, vacancy))

        if not candidates:
            return stats

        # --- 5: обогащение, параллельно ---
        enriched = await _gather_limited(
            [enrich_vacancy(v) for _, v in candidates], CONCURRENCY
        )
        stats.enriched = len(enriched)
        pairs = list(zip([p for p, _ in candidates], enriched))

        # --- 6: фильтр (правила бесплатны, оценка — вызов), параллельно ---
        results: list[FilterResult] = await _gather_limited(
            [filter_vacancy(v, criteria) for _, v in pairs], CONCURRENCY
        )

        # --- 7: дедуп по содержимому и доставка ---
        for (post, vacancy), result in zip(pairs, results):
            if result.score is None and result.reasons:
                stats.cut_by_rules += 1
                continue
            stats.assessed += 1
            if result.score:
                stats.scores[result.score.value] = (
                    stats.scores.get(result.score.value, 0) + 1
                )
            if not result.passed:
                continue

            hash_value = content_hash(post.text)
            if exists(conn, hash_value):
                stats.duplicates += 1
                continue

            save_vacancy(
                conn,
                hash_value=hash_value,
                channel=post.channel,
                message_id=post.message_id,
                vacancy=vacancy,
                score=result.score.value if result.score else None,
                reasoning=result.reasoning,
                link=post.link,
            )
            conn.commit()

            if bot is not None and await send_vacancy(bot, vacancy, result, post.link):
                mark_delivered(conn, hash_value)
                conn.commit()
                stats.delivered += 1

        return stats
    finally:
        conn.close()
