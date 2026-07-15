"""
Оркестратор фильтра: дешёвые правила -> дорогая ИИ-оценка.

Порядок не косметика: правила чистый Python и бесплатны, оценка — вызов API.
Не прошёл правила -> ИИ не зовём вообще.
"""

from pydantic import BaseModel

from src.filters.criteria import Criteria
from src.filters.relevance import RelevanceResult, Score, assess_relevance
from src.filters.rules import passes_hard_rules
from src.models.vacancy import Vacancy


class FilterResult(BaseModel):
    passed: bool  # прошла ли вакансия фильтр целиком
    reasons: list[str] = []  # чем отсекли (пусто, если прошла правила)
    score: Score | None = None  # None -> ИИ не звали или он не ответил
    reasoning: str | None = None


async def filter_vacancy(vacancy: Vacancy, criteria: Criteria) -> FilterResult:
    passed_rules, reasons = passes_hard_rules(vacancy, criteria)
    if not passed_rules:
        return FilterResult(passed=False, reasons=reasons)

    assessment: RelevanceResult | None = await assess_relevance(vacancy, criteria)
    if assessment is None:
        # ИИ не ответил — вакансию не теряем: правила она прошла, покажем
        # без оценки, а не выкинем молча
        return FilterResult(passed=True, reasoning="оценка не удалась (ошибка API)")

    # low = правила пропустили, но по сути мимо -> в выдачу не идёт.
    # no_data проходит ОСОЗНАННО: пост-заглушка это "не знаем", а не "не
    # подходит", и за ссылкой вполне может быть отличная вакансия. В карточке
    # он помечен отдельным бейджем, так что с medium не спутать.
    return FilterResult(
        passed=assessment.score is not Score.LOW,
        score=assessment.score,
        reasoning=assessment.reasoning,
    )
