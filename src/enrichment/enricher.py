"""
ИИ-слой: дозаполняет то, что не взяли регулярки. Один пост = один вызов API.

Принцип: регулярки — источник истины для того, что они взяли уверенно
(title/work_format/contact/hashtags). ИИ их НЕ переписывает, а только заполняет
пустое. Исключение — нормализованная зарплата и is_vacancy: их ИИ считает
всегда, потому что регулярки нормализацию не делают в принципе.

Ошибка API на одном посте не роняет прогон: ловим, помечаем enrichment_failed
в parse_flags, отдаём исходную модель нетронутой, идём дальше.
"""

import os

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

3. Зарплату нормализуй в рубли за месяц:
   - "160к", "160k" -> 160000; "210.000" -> 210000; "180 000" -> 180000; \
"80 тыс" -> 80000
   - голое число в контексте зарплаты — это тысячи: "102 гросс" -> 102000
   - "гросс"/"gross" -> gross=true; "на руки"/"net"/"нетто" -> gross=false; \
не сказано -> null
   - если вилок несколько (например ИП vs ТК, или час vs месяц) — основной \
считай месячную по ТК; остальные положи в salary_alternatives как подстроки из поста
   - ставку в час НЕ переводи в месяц: period="hour" и значение как есть

4. Ничего не выдумывай. Нет данных в посте — null. Компанию бери только если \
она названа; "крупный банк" — это не название, это null."""


def _build_prompt(vacancy: Vacancy) -> str:
    return f"Извлеки данные из поста:\n\n<post>\n{vacancy.raw_text}\n</post>"


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Клиент создаётся один раз и переиспользуется (внутри — пул соединений)."""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY не найден в .env — добавь ключ и повтори"
            )
        _client = anthropic.Anthropic()
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


def enrich_vacancy(vacancy: Vacancy) -> Vacancy:
    """
    Один пост -> один вызов Claude -> дозаполненная Vacancy.

    Не бросает исключений: любая ошибка (сеть, rate limit, кривой ответ)
    помечается в parse_flags, возвращается исходная модель.
    """
    try:
        response = _get_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
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

    result = response.parsed_output
    if result is None:
        # structured output не дал объект — например упёрлись в max_tokens
        return _failed(vacancy, f"no_parsed_output (stop={response.stop_reason})")

    return _merge(vacancy, result)


def _failed(vacancy: Vacancy, reason: str) -> Vacancy:
    failed = vacancy.model_copy(deep=True)
    failed.parse_flags = [*failed.parse_flags, f"enrichment_failed: {reason}"]
    return failed
