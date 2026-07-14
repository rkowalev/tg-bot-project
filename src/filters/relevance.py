"""
ИИ-оценка релевантности — второй уровень фильтра. Один вызов Haiku.

Зовётся ТОЛЬКО для вакансий, прошедших жёсткие правила: платить за оценку
заведомо неподходящих незачем.

Что ловит этого правила не видят: смысловое соответствие. "QA с базовыми
знаниями Java" формально мимо Python-стека, но по сути это позиция, куда берут
и переучивают. Правила такое режут, ИИ — объясняет.

score — enum, а не число 0-100: у LLM трёхбалльная шкала воспроизводима, а
"73 из 100" — ложная точность, которая на следующем прогоне станет 68 без
всякой причины.
"""

import os
from enum import Enum

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from src.filters.criteria import Criteria
from src.models.vacancy import Vacancy

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024


class Score(str, Enum):
    HIGH = "high"  # подходит, откликаться
    MEDIUM = "medium"  # спорно, глянуть глазами
    LOW = "low"  # мимо, но правила пропустили


class RelevanceResult(BaseModel):
    score: Score = Field(
        description="high — подходит, стоит откликаться. medium — спорно, надо смотреть глазами. low — формально прошло правила, но по сути мимо."
    )
    reasoning: str = Field(
        description="Одно-два предложения по-русски: почему подходит или чего не хватает. Конкретно, со ссылкой на факты вакансии."
    )


class _Stats:
    """Счётчик вызовов — DoD требует доказать, что ИИ не зовётся на отсеянных."""

    calls: int = 0
    failures: int = 0


STATS = _Stats()

# TODO: клиент и его прогрев дублируют enrichment/enricher.py. Свести в общий
# модуль, когда будет разрешено трогать enrichment (сейчас он вне scope).
_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY не найден в .env")
        _client = anthropic.AsyncAnthropic()
    return _client


def _system_prompt(criteria: Criteria) -> str:
    """
    Критерии кладём СЮДА, а не в сообщение: за прогон они не меняются, значит
    вместе с инструкцией попадают в кэшируемый префикс. В сообщении — только
    вакансия, она меняется на каждом вызове.
    """
    return f"""\
Ты помогаешь QA-инженеру отбирать вакансии. Тебе дают вакансию, ты оцениваешь, \
насколько она подходит под критерии ниже, и коротко объясняешь почему.

Критерии пользователя:
- языки, на которых он пишет: {criteria.languages or "любые"}
- зарплата от: {f"{criteria.min_salary // 1000}к руб/мес" if criteria.min_salary else "не важна"}
- форматы работы: {[f.value for f in criteria.work_formats] or "любой"}
- знакомые инструменты: {criteria.stack_include or "любые"}
- грейды: {[g.value for g in criteria.grades] or "любой (важна зарплата, а не название грейда)"}

Главное, что нужно понять про этого пользователя:

ЯЗЫК — жёсткое требование. Новый язык программирования он учить НЕ будет. \
Если основной язык вакансии не из его списка — это low, даже если всё \
остальное идеально. Внимательно: язык, упомянутый вскользь ("Python для \
вспомогательных скриптов" в Kotlin-вакансии), основным НЕ считается — такая \
вакансия тоже low.

ИНСТРУМЕНТЫ — мягкое. Незнакомый Docker, Kubernetes, новый фреймворк — не \
проблема, выучить вопрос недели. НЕ понижай за них оценку.

ГРЕЙД — сам по себе не важен. Вакансия на middle с хорошей зарплатой \
устраивает не меньше, чем senior. Смотри на деньги и на суть задач, а не на \
название уровня.

Как оценивать:

high — язык тот, зарплата в порядке, формат подходит. Незнакомые инструменты \
на оценку не влияют.

medium — язык тот, но есть заметное расхождение: зарплата не указана или на \
границе, требования размыты, задачи частично не про то.

low — язык не тот (или Python в вакансии только для вспомогательных скриптов), \
либо это вообще не про тестирование.

Если зарплата не указана — это минус в сторону medium, но не приговор.

reasoning: одно-два предложения, конкретно, со ссылкой на факты вакансии. \
Не пересказывай вакансию — объясняй решение."""


def _user_prompt(vacancy: Vacancy) -> str:
    salary = "не указана"
    if vacancy.salary and vacancy.salary.min_value:
        gross = {True: "гросс", False: "на руки", None: "?"}[vacancy.salary.gross]
        salary = (
            f"{vacancy.salary.min_value // 1000}-"
            f"{(vacancy.salary.max_value or vacancy.salary.min_value) // 1000}к {gross}"
        )
    return f"""\
Оцени вакансию.

Разобранные поля:
- должность: {vacancy.title or "не распознана"}
- компания: {vacancy.company or "не указана"}
- грейд: {vacancy.grade.value if vacancy.grade else "неизвестен"}
- формат: {vacancy.work_format.value if vacancy.work_format else "неизвестен"}
- зарплата: {salary}
- стек: {vacancy.stack or "не распознан"}

Полный текст поста:
<post>
{vacancy.raw_text}
</post>"""


async def assess_relevance(vacancy: Vacancy, criteria: Criteria) -> RelevanceResult | None:
    """
    Оценка релевантности. None — если вызов не удался (прогон не роняем,
    вакансию покажем без оценки, чем потеряем молча).
    """
    STATS.calls += 1
    try:
        response = await _get_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # ЗАМЕРЕНО: сейчас кэш тут НЕ работает. Префикс (схема ~750 +
            # инструкция с критериями ~544) = ~1300 токенов при пороге 4096 у
            # Haiku 4.5 — кэш молча не включается, creation=0/read=0.
            # В enrichment он работает только потому, что там схема Vacancy
            # тянет 3.4k токенов, а RelevanceResult — это enum и строка.
            # Оставлено осознанно: цена вопроса ~$0.05 за прогон (оценка идёт
            # лишь на ~12% постов), а разметка включится сама, если промпт
            # вырастет. Раздувать его ради кэша смысла нет.
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(criteria),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": _user_prompt(vacancy)}],
            output_format=RelevanceResult,
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError, ValidationError):
        STATS.failures += 1
        return None

    return response.parsed_output
