"""
Критерии поиска — ДАННЫЕ, не код. Фильтр их не знает, ему передают параметром.

На Итерации 5 часть этих полей заполнит парсер резюме (stack_include, grades —
"что я умею"). min_salary и work_formats из резюме не выводятся: это
предпочтения, а не факты о кандидате, — их всё равно задаёт пользователь.

Технологии проверяются против config/stack.py при импорте: опечатка или
неизвестное слово упадёт здесь, а не выльется в молча пустую выдачу.
"""

from src.filters.criteria import Criteria
from src.models.vacancy import Grade, WorkFormat

RUSLAN = Criteria(
    work_formats=[WorkFormat.REMOTE],
    # 200к для отладки: даёт ~15 вакансий/мес против ~8 на целевых 270к —
    # на таком потоке видно, как работает фильтр. Поднять к цели после Итерации 4.
    min_salary=200_000,
    stack_include=["Python", "Playwright", "API", "Selenium", "pytest"],
    stack_exclude=[],
    grades=[Grade.SENIOR, Grade.MIDDLE],
)
