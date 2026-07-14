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
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY не найден в .env")
        _client = anthropic.Anthropic()
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
- форматы работы: {[f.value for f in criteria.work_formats] or "любой"}
- зарплата от: {f"{criteria.min_salary // 1000}к руб/мес" if criteria.min_salary else "не важна"}
- желаемые технологии: {criteria.stack_include or "любые"}
- стоп-технологии: {criteria.stack_exclude or "нет"}
- грейды: {[g.value for g in criteria.grades] or "любой"}

Как оценивать:

high — вакансия по сути подходит: стек в основном совпадает, грейд и условия \
в рамках критериев. Мелкие расхождения (одна незнакомая технология, зарплата \
на границе) — не повод понижать.

medium — есть заметное расхождение, но вакансия не бессмысленна: часть стека \
чужая, грейд соседний, зарплата не указана, требования размыты. Сюда же — \
случаи вроде "QA с базовыми знаниями Java" при Python-стеке: формально мимо, \
но это позиция, где готовы переучивать.

low — по сути мимо, хотя формальные правила пропустили: основной стек чужой, \
грейд сильно не тот, или это вообще не про тестирование.

Что учитывать помимо формального совпадения: готовы ли переучивать, насколько \
жёсткие требования, есть ли в вакансии то, чего пользователь явно не хочет. \
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


def assess_relevance(vacancy: Vacancy, criteria: Criteria) -> RelevanceResult | None:
    """
    Оценка релевантности. None — если вызов не удался (прогон не роняем,
    вакансию покажем без оценки, чем потеряем молча).
    """
    STATS.calls += 1
    try:
        response = _get_client().messages.parse(
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
