"""
Тесты профиля из резюме. Вызов ИИ подменён — проверяем маппинг и защиту.
"""

import pytest

from src.filters.criteria import Criteria
from src.filters.rules import passes_hard_rules
from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat
from src.profile.resume_parser import ResumeProfile, _to_criteria, describe

from datetime import datetime

NOW = datetime(2026, 1, 1)


def _profile(**kwargs) -> ResumeProfile:
    base = dict(
        languages=[], tools=[], grade=Grade.UNKNOWN, work_formats=[], min_salary=None
    )
    return ResumeProfile(**{**base, **kwargs})


# ---------- маппинг резюме -> Criteria ----------


def test_languages_and_tools_are_separated():
    criteria = _to_criteria(_profile(languages=["Python"], tools=["Playwright", "Docker"]))
    assert criteria.languages == ["Python"], "язык — жёсткий критерий, отдельно"
    assert criteria.stack_include == ["Playwright", "Docker"], "инструменты — мягкие"


def test_grade_from_resume_does_not_filter():
    """
    Владелец: "есть вакансии на мидла, но платят 250к — меня устраивает".
    Резюме говорит "я senior" — это факт о кандидате, а не требование к
    вакансии. Фильтровать по нему = терять деньги.
    """
    criteria = _to_criteria(_profile(grade=Grade.SENIOR))
    assert criteria.grades == [], "грейдом не фильтруем, реальный критерий — зарплата"


def test_language_in_tools_slot_is_dropped():
    # если модель перепутала колонки, язык не должен утечь в мягкие критерии
    criteria = _to_criteria(_profile(languages=["Python"], tools=["Java", "Docker"]))
    assert criteria.stack_include == ["Docker"]


def test_salary_and_formats_carried_over():
    criteria = _to_criteria(
        _profile(min_salary=270_000, work_formats=[WorkFormat.REMOTE])
    )
    assert criteria.min_salary == 270_000
    assert criteria.work_formats == [WorkFormat.REMOTE]


# ---------- защита от технологий не из словаря ----------


def test_unknown_technology_is_dropped_not_raised():
    """
    Резюме почти наверняка содержит что-то за пределами config/stack.py.
    Criteria на таком падает — онбординг из-за этого падать не должен.
    """
    criteria = _to_criteria(
        _profile(languages=["Python"], tools=["Katalon", "SoapUI", "pytest"])
    )
    assert criteria.stack_include == ["pytest"], "незнакомое молча отбрасываем"


def test_technology_case_is_normalized():
    criteria = _to_criteria(_profile(languages=["PYTHON"], tools=["playwright"]))
    assert criteria.languages == ["Python"]
    assert criteria.stack_include == ["Playwright"]


def test_language_aliases_from_resume():
    criteria = _to_criteria(_profile(languages=["golang", "python"]))
    assert criteria.languages == ["Go", "Python"]


def test_all_stack_unknown_gives_empty_not_crash():
    criteria = _to_criteria(_profile(tools=["Katalon", "Robot Framework"]))
    assert criteria.stack_include == []


# ---------- describe: это видит человек на подтверждении ----------


def test_describe_is_human_readable():
    text = describe(
        Criteria(
            languages=["Python"],
            stack_include=["Playwright", "Docker"],
            work_formats=[WorkFormat.REMOTE],
            min_salary=230_000,
        )
    )
    assert "Python" in text
    assert "от 230к" in text
    # человек должен понимать, что жёстко, а что нет
    assert "отсекаю" in text
    assert "доучить" in text


def test_describe_handles_empty_criteria():
    text = describe(Criteria())
    assert "не распознан" in text


# ---------- ГЛАВНОЕ: задел с итерации 3 сработал ----------


def test_criteria_from_resume_work_in_filter_unchanged():
    """
    Смысл всей итерации 5: фильтр принимает Criteria извне и не знает, откуда
    они. Критерии из резюме должны работать в НЕТРОНУТОМ фильтре.
    """
    criteria = _to_criteria(
        _profile(
            languages=["Python"],
            tools=["Playwright"],
            grade=Grade.SENIOR,
            work_formats=[WorkFormat.REMOTE],
            min_salary=230_000,
        )
    )

    good = Vacancy(
        raw_text="x",
        posted_at=NOW,
        is_vacancy=True,
        work_format=WorkFormat.REMOTE,
        grade=Grade.MIDDLE,  # мидл с хорошей зарплатой — устраивает
        stack=["Python", "pytest"],
        salary=Salary(raw="250к", min_value=250_000, max_value=250_000),
    )
    passed, reasons = passes_hard_rules(good, criteria)
    assert passed is True, f"мидл за 250к должен пройти: {reasons}"

    java = good.model_copy(update={"stack": ["Java", "Selenium"]})
    passed, reasons = passes_hard_rules(java, criteria)
    assert passed is False, "Java-вакансия должна отсекаться правилами"
    assert "язык Java" in reasons[0]

    bad = good.model_copy(update={"work_format": WorkFormat.OFFICE})
    passed, _ = passes_hard_rules(bad, criteria)
    assert passed is False, "офис при удалёнке в критериях должен отсекаться"
