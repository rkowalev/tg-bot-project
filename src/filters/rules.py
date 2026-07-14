"""
Жёсткие правила — первый уровень фильтра. Чистый Python, без сети и ИИ.

Принцип: правила отсекают только то, что заведомо не подходит по фактам.
Смысловую оценку ("формально не мой стек, но позиция junior-friendly") они не
тянут — это работа второго уровня (relevance.py).

Главное правило про неизвестность: НЕИЗВЕСТНО != НЕ ПОДХОДИТ. Вакансию с
неразобранной зарплатой или грейдом не отсекаем — пропускаем на ИИ, пусть он
разбирается по тексту. Иначе фильтр молча съест подходящие вакансии просто
потому, что автор поста написал зарплату нестандартно.
"""

from config.stack import languages_in
from src.filters.criteria import Criteria
from src.models.vacancy import Grade, Vacancy, WorkFormat


def passes_prefilter(vacancy: Vacancy, criteria: Criteria) -> bool:
    """
    Отсев ДО обогащения — на данных одних регулярок. Экономит вызов ИИ.

    Проверяем только формат работы и язык: их регулярки берут надёжно
    (формат ~95%, язык по словарю). Зарплату и грейд здесь трогать НЕЛЬЗЯ —
    именно их чинит ИИ (грейд у регулярок верен лишь в ~54% случаев), и отсев
    по ним на сырых данных выкинул бы вакансии, которые обогащение исправило бы.

    is_vacancy тоже не смотрим — его знает только ИИ, до обогащения он None.
    """
    if (
        criteria.work_formats
        and vacancy.work_format is not None
        and vacancy.work_format is not WorkFormat.UNKNOWN
        and vacancy.work_format not in criteria.work_formats
    ):
        return False

    # язык — жёсткий критерий и регулярки берут его надёжно (по словарю),
    # значит чужой ЯП можно отсечь ещё до обогащения, бесплатно
    if _language_reasons(vacancy, criteria):
        return False

    return True


def passes_hard_rules(vacancy: Vacancy, criteria: Criteria) -> tuple[bool, list[str]]:
    """
    Возвращает (прошёл, причины отказа). Причины — для отладки: видно, какое
    правило отсекло вакансию, а не просто "не прошла".
    """
    reasons: list[str] = []

    # ИИ на Итерации 2 уже отличил вакансию от спама/служебки — доверяем.
    # Без этой проверки мусор доехал бы до ПЛАТНОЙ ИИ-оценки релевантности.
    if vacancy.is_vacancy is False:
        reasons.append("не вакансия (спам/служебный пост)")
        return False, reasons

    # UNKNOWN у формата и грейда — это "не распознали", а не "не подходит".
    # Отсекать по нему нельзя ровно по той же причине, что и по зарплате.
    if (
        criteria.work_formats
        and vacancy.work_format is not None
        and vacancy.work_format is not WorkFormat.UNKNOWN
        and vacancy.work_format not in criteria.work_formats
    ):
        allowed = "/".join(f.value for f in criteria.work_formats)
        reasons.append(f"формат {vacancy.work_format.value} не входит в {allowed}")

    if (
        criteria.grades
        and vacancy.grade is not None
        and vacancy.grade is not Grade.UNKNOWN
        and vacancy.grade not in criteria.grades
    ):
        allowed = "/".join(g.value for g in criteria.grades)
        reasons.append(f"грейд {vacancy.grade.value} не входит в {allowed}")

    reasons.extend(_salary_reasons(vacancy, criteria))
    reasons.extend(_language_reasons(vacancy, criteria))
    reasons.extend(_stack_reasons(vacancy, criteria))

    return not reasons, reasons


def _salary_reasons(vacancy: Vacancy, criteria: Criteria) -> list[str]:
    if criteria.min_salary is None:
        return []
    # зарплаты нет или числа не разобраны -> неизвестно, а не мало
    if vacancy.salary is None or vacancy.salary.max_value is None:
        return []
    # сравниваем по верхней границе вилки: "160-210к" при пороге 200к проходит,
    # потому что договориться на 210 реально
    if vacancy.salary.max_value < criteria.min_salary:
        return [
            f"зарплата до {vacancy.salary.max_value // 1000}к "
            f"ниже порога {criteria.min_salary // 1000}к"
        ]
    return []


def _language_reasons(vacancy: Vacancy, criteria: Criteria) -> list[str]:
    """
    Язык — единственный жёсткий критерий по стеку.

    Логика ровно как у человека: "python/go" — годится, там есть Python;
    "только go" — нет, учить новый язык ради вакансии никто не станет.

    Язык в посте не назван -> неизвестность, а не отказ (как с зарплатой).
    """
    if not criteria.languages:
        return []

    vacancy_languages = languages_in(vacancy.stack)
    if not vacancy_languages:
        return []
    if vacancy_languages & set(criteria.languages):
        return []

    return [
        f"язык {'/'.join(sorted(vacancy_languages))} — не из твоих "
        f"({'/'.join(criteria.languages)})"
    ]


def _stack_reasons(vacancy: Vacancy, criteria: Criteria) -> list[str]:
    """
    ИНСТРУМЕНТЫ ПРАВИЛАМИ НЕ ОТСЕКАЮТ.

    Раньше здесь было "стек не пересёкся с желаемым" — и это выкидывало
    вакансии, где просто нужен незнакомый Docker. Выучить инструмент — вопрос
    недели, а вакансию мы теряли навсегда. Жёсткий критерий теперь только язык
    (_language_reasons), а инструменты ушли в ИИ-оценку как контекст.

    stack_exclude оставлен: он про "не хочу вообще", а не про "не умею".
    """
    if not criteria.stack_exclude:
        return []

    stack = {tech.lower() for tech in vacancy.stack}
    excluded = {tech.lower() for tech in criteria.stack_exclude}
    # отсекаем, только если стек ЦЕЛИКОМ из стоп-технологий
    if stack and stack <= excluded:
        return [f"стек целиком из стоп-технологий: {sorted(stack)}"]
    return []
