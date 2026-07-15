"""
ИИ-слой: дозаполняет то, что не взяли регулярки. Один пост = один вызов API.

Принцип: регулярки — источник истины для того, что они взяли уверенно
(title/work_format/contact/hashtags). ИИ их НЕ переписывает, а только заполняет
пустое. Исключение — нормализованная зарплата и is_vacancy: их ИИ считает
всегда, потому что регулярки нормализацию не делают в принципе.

Ошибка API на одном посте не роняет прогон: ловим, помечаем enrichment_failed
в parse_flags, отдаём исходную модель нетронутой, идём дальше.

Клиент АСИНХРОННЫЙ: конвейер обрабатывает посты пачкой, и синхронный вызов
внутри async-цикла блокировал бы всё. Замерено: 10 постов последовательно —
21 с, параллельно по 10 — 2.9 с (7.4x).
"""

import os
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv
from pydantic import ValidationError

from src.enrichment.schema import EnrichmentResult
from src.models.vacancy import Grade, Salary, Vacancy

load_dotenv()

# Задача простая (извлечение из короткого текста) — мощная модель не нужна.
# Константой, чтобы менять в одном месте.
MODEL = "claude-haiku-4-5-20251001"

# Ответ — небольшой JSON-объект. Запас на длинные названия компаний и вилки.
MAX_TOKENS = 2048

SYSTEM_PROMPT = """\
Ты извлекаешь структурированные данные из постов русскоязычных Telegram-каналов \
с IT-вакансиями (в основном QA/тестирование). Посты пишут разные люди в свободной \
форме, поэтому формат плавает.

Правила:

1. is_vacancy=false для всего, что не является вакансией: реклама услуг, флуд, \
служебные сообщения модератора ("укажите вилку", "вакансия будет удалена", \
"флуд карается баном"), обсуждения. Для таких постов остальные поля = null.

2. grade выводи по совокупности признаков, а не по одному ключевому слову: \
требуемый опыт в годах (до 1 года -> junior, 1-3 -> middle, 3-5 -> senior, \
руководство командой -> lead), формулировки требований, заголовок. Если в посте \
перечислено несколько грейдов (например "middle/senior") — выбери нижний. \
Если признаков нет — unknown. Не угадывай.

3. Зарплату разверни в число, но НЕ КОНВЕРТИРУЙ ВАЛЮТУ:
   - "160к", "160k" -> 160000; "210.000" -> 210000; "180 000" -> 180000; \
"80 тыс" -> 80000
   - голое число в контексте зарплаты — это тысячи: "102 гросс" -> 102000. \
Это правило ТОЛЬКО для рублей. При валюте бери число как написано: \
"2 500 $" -> 2500 + currency="USD", а НЕ 2500000. "$2,5k" -> 2500.
   - currency ставь по значку в посте: $ -> "USD", € -> "EUR", ₽/руб/р. или \
значка нет -> "RUB". Пересчитывать в рубли НЕ НАДО — курса ты не знаешь, \
это сделает код. Твоё дело — извлечь ровно то, что написано.
   - "гросс"/"gross" -> gross=true; "на руки"/"net"/"нетто" -> gross=false; \
не сказано -> null
   - если вилок несколько (например ИП vs ТК, или час vs месяц) — основной \
считай месячную по ТК; остальные положи в salary_alternatives как подстроки из поста
   - ставку в час НЕ переводи в месяц: period="hour" и значение как есть

4. Ничего не выдумывай. Нет данных в посте — null. Компанию бери только если \
она названа; "крупный банк" — это не название, это null.

Примеры на форматах, которые в этом канале встречаются чаще всего:

Вход: "ЗП: 102 гросс"
Выход: min=102000, max=102000, gross=true, period=month
Почему: голое число в контексте зарплаты — это тысячи, а не 102 рубля.

Вход: "Вилка: ИП 150 гросс / по ТК 100к гросс"
Выход: min=100000, max=100000, gross=true, period=month,
       salary_alternatives=["ИП 150 гросс"]
Почему: вилки две, основная — по ТК; вторая уходит в alternatives как есть.

Вход: "-ИП (руб/час): 1200 до 1500
      -ТК РФ (руб, net): 180 000 - 220 000"
Выход: min=180000, max=220000, gross=false, period=month,
       salary_alternatives=["ИП (руб/час): 1200 до 1500"]
Почему: месячная по ТК основная; часовую ставку в месяц НЕ пересчитываем.

Вход: "Заработная плата: 80 тыс. руб. на руки"
Выход: min=80000, max=80000, gross=false, period=month

Вход: "Уровень зп: 240k net."
Выход: min=240000, max=240000, gross=false, period=month

Вход: "Вилка: до 213 к гросс"
Выход: min=213000, max=213000, gross=true, period=month

Вход: "Опыт работы в QA: от 4 лет" (грейд словом не назван)
Выход: grade=senior
Почему: грейд выводится из требуемого опыта, даже если слова senior в посте нет.

Вход: "#вакансия #middle #senior ... Опыт работы: от 2 лет"
Выход: grade=middle
Почему: перечислено несколько грейдов — берём нижний.

Вход: "Укажите название компании или вилку, или вакансия будет удалена"
Выход: is_vacancy=false, остальные поля null
Почему: это служебное сообщение модератора, а не вакансия."""


def _build_prompt(vacancy: Vacancy) -> str:
    return f"Извлеки данные из поста:\n\n<post>\n{vacancy.raw_text}\n</post>"


@dataclass
class CacheStats:
    """Счётчики usage за прогон — диагностика цены, не часть контракта модели."""

    calls: int = 0
    cache_creation: int = 0  # токены, записанные в кэш (стоят 1.25x)
    cache_read: int = 0  # токены, прочитанные из кэша (стоят 0.1x)
    uncached_input: int = 0  # токены мимо кэша (полная цена) — это текст поста

    @property
    def input_without_cache(self) -> int:
        """Сколько входных токенов заплатили бы без кэша вообще."""
        return self.uncached_input + self.cache_creation + self.cache_read

    @property
    def effective_input(self) -> int:
        """Во сколько токенов по цене обошёлся прогон с кэшем."""
        return int(self.uncached_input + self.cache_creation * 1.25 + self.cache_read * 0.1)


STATS = CacheStats()


def reset_stats() -> None:
    global STATS
    STATS = CacheStats()


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Клиент создаётся один раз и переиспользуется (внутри — пул соединений)."""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY не найден в .env — добавь ключ и повтори"
            )
        _client = anthropic.AsyncAnthropic()
    return _client


def _merge(vacancy: Vacancy, result: EnrichmentResult) -> Vacancy:
    """
    Кладёт ответ ИИ поверх регулярочного результата.

    Регулярочные поля не трогаем — заполняем только пустые. Salary и is_vacancy
    берём у ИИ всегда (см. модуль-docstring).
    """
    enriched = vacancy.model_copy(deep=True)

    enriched.is_vacancy = result.is_vacancy
    filled_by_ai: list[str] = []

    if enriched.title is None and result.title:
        enriched.title = result.title
        filled_by_ai.append("title")
    if enriched.company is None and result.company:
        enriched.company = result.company

    # Регулярки пишут UNKNOWN, когда не нашли — для грейда это то же "пусто".
    if enriched.grade in (None, Grade.UNKNOWN):
        enriched.grade = result.grade
        if result.grade is not Grade.UNKNOWN:
            filled_by_ai.append("grade")

    if result.salary is not None:
        # raw от регулярок точнее (это буквальная подстрока), поэтому в
        # приоритете; если регулярки зарплату не нашли вовсе — берём raw у ИИ.
        raw = vacancy.salary.raw if vacancy.salary else result.salary.raw
        if raw:
            enriched.salary = Salary(
                min_value=result.salary.min_value,
                max_value=result.salary.max_value,
                currency=result.salary.currency,
                gross=result.salary.gross,
                period=result.salary.period,
                raw=raw,
            )
            if vacancy.salary is None:
                filled_by_ai.append("salary")

    enriched.salary_alternatives = result.salary_alternatives
    enriched.parse_flags = _resolve_flags(enriched.parse_flags, filled_by_ai)

    return enriched


def _resolve_flags(flags: list[str], filled_by_ai: list[str]) -> list[str]:
    """
    Снимает *_not_parsed по полям, которые заполнил ИИ, и ставит вместо них
    enriched_by_ai:<поле>.

    Зачем: флаг "salary_not_parsed" рядом с заполненной зарплатой — враньё,
    а на этой модели дальше будет строиться фильтр (Итерация 3). При этом
    информация не теряется: enriched_by_ai показывает, что регулярки поле не
    взяли, а взял ИИ. Итого not_parsed + enriched_by_ai = прежний baseline.
    """
    resolved = [
        flag
        for flag in flags
        if flag.split(":", 1)[0] not in {f"{field}_not_parsed" for field in filled_by_ai}
    ]
    return resolved + [f"enriched_by_ai: {field}" for field in filled_by_ai]


async def enrich_vacancy(vacancy: Vacancy) -> Vacancy:
    """
    Один пост -> один вызов Claude -> дозаполненная Vacancy.

    Не бросает исключений: любая ошибка (сеть, rate limit, кривой ответ)
    помечается в parse_flags, возвращается исходная модель.
    """
    try:
        response = await _get_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # cache_control кэширует ВЕСЬ префикс до этой точки: сначала схема
            # structured output (~3.4k токенов), затем системный промпт (~0.8k).
            # Текст поста идёт в messages, то есть ПОСЛЕ точки — он меняется на
            # каждом вызове и в кэш не попадает, иначе кэш промахивался бы всегда.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": _build_prompt(vacancy)}],
            output_format=EnrichmentResult,
        )
    except anthropic.RateLimitError as error:
        return _failed(vacancy, f"rate_limit: {error}")
    except anthropic.APIStatusError as error:
        return _failed(vacancy, f"api_{error.status_code}: {error.message}")
    except anthropic.APIConnectionError as error:
        return _failed(vacancy, f"connection: {error}")
    except ValidationError as error:
        return _failed(vacancy, f"invalid_response: {error}")

    usage = response.usage
    STATS.calls += 1
    STATS.cache_creation += usage.cache_creation_input_tokens or 0
    STATS.cache_read += usage.cache_read_input_tokens or 0
    STATS.uncached_input += usage.input_tokens

    result = response.parsed_output
    if result is None:
        # structured output не дал объект — например упёрлись в max_tokens
        return _failed(vacancy, f"no_parsed_output (stop={response.stop_reason})")

    return _merge(vacancy, result)


def _failed(vacancy: Vacancy, reason: str) -> Vacancy:
    failed = vacancy.model_copy(deep=True)
    failed.parse_flags = [*failed.parse_flags, f"enrichment_failed: {reason}"]
    return failed
