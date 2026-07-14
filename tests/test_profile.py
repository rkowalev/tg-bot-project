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
    base = dict(stack=[], grade=Grade.UNKNOWN, work_formats=[], min_salary=None)
    return ResumeProfile(**{**base, **kwargs})


# ---------- маппинг резюме -> Criteria ----------


def test_stack_becomes_stack_include():
    criteria = _to_criteria(_profile(stack=["Python", "Playwright"]))
    assert criteria.stack_include == ["Python", "Playwright"]


def test_grade_becomes_single_element_list():
    criteria = _to_criteria(_profile(grade=Grade.SENIOR))
    assert criteria.grades == [Grade.SENIOR]


def test_unknown_grade_gives_empty_list_not_unknown():
    # пустой список = критерий не применяется; [UNKNOWN] отсекал бы всё подряд
    criteria = _to_criteria(_profile(grade=Grade.UNKNOWN))
    assert criteria.grades == []


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
    criteria = _to_criteria(_profile(stack=["Python", "Katalon", "SoapUI", "pytest"]))
    assert criteria.stack_include == ["Python", "pytest"], "незнакомое молча отбрасываем"


def test_technology_case_is_normalized():
    criteria = _to_criteria(_profile(stack=["PYTHON", "playwright"]))
    assert criteria.stack_include == ["Python", "Playwright"]


def test_all_stack_unknown_gives_empty_not_crash():
    criteria = _to_criteria(_profile(stack=["Katalon", "Robot Framework"]))
    assert criteria.stack_include == []


# ---------- describe: это видит человек на подтверждении ----------


def test_describe_is_human_readable():
    text = describe(
        Criteria(
            stack_include=["Python", "Playwright"],
            grades=[Grade.SENIOR],
            work_formats=[WorkFormat.REMOTE],
            min_salary=270_000,
        )
    )
    assert "Python, Playwright" in text
    assert "senior" in text
    assert "от 270к" in text


def test_describe_handles_empty_criteria():
    text = describe(Criteria())
    assert "не распознан" in text
    assert "любой" in text


# ---------- ГЛАВНОЕ: задел с итерации 3 сработал ----------


def test_criteria_from_resume_work_in_filter_unchanged():
    """
    Смысл всей итерации 5: фильтр принимает Criteria извне и не знает, откуда
    они. Критерии из резюме должны работать в НЕТРОНУТОМ фильтре.
    """
    criteria = _to_criteria(
        _profile(
            stack=["Python", "Playwright"],
            grade=Grade.SENIOR,
            work_formats=[WorkFormat.REMOTE],
            min_salary=270_000,
        )
    )

    good = Vacancy(
        raw_text="x",
        posted_at=NOW,
        is_vacancy=True,
        work_format=WorkFormat.REMOTE,
        grade=Grade.SENIOR,
        stack=["Python", "pytest"],
        salary=Salary(raw="300к", min_value=300_000, max_value=300_000),
    )
    passed, reasons = passes_hard_rules(good, criteria)
    assert passed is True, f"подходящая вакансия должна пройти: {reasons}"

    bad = good.model_copy(update={"work_format": WorkFormat.OFFICE})
    passed, _ = passes_hard_rules(bad, criteria)
    assert passed is False, "офис при удалёнке в критериях должен отсекаться"
