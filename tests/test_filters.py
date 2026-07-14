"""
Тесты жёстких правил и оркестратора. Без сети: правила чистый Python, а
ИИ-оценку в оркестраторе подменяем.
"""

from datetime import datetime

import pytest

from src.filters.criteria import Criteria
from src.filters.rules import passes_hard_rules
from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat

NOW = datetime(2026, 1, 1)


def _vacancy(**kwargs) -> Vacancy:
    base = dict(raw_text="текст", posted_at=NOW, is_vacancy=True)
    return Vacancy(**{**base, **kwargs})


# ---------- критерии валидируются против словаря ----------


def test_criteria_rejects_unknown_technology():
    # смысл всей правки: критерий по технологии, которую парсер не извлекает,
    # молча не сматчился бы никогда
    with pytest.raises(ValueError, match="не в config/stack.py"):
        Criteria(stack_include=["pytest-bdd"])


def test_criteria_normalizes_technology_case():
    criteria = Criteria(stack_include=["PYTHON", "playwright"])
    assert criteria.stack_include == ["Python", "Playwright"]


def test_empty_criteria_pass_everything():
    passed, reasons = passes_hard_rules(_vacancy(), Criteria())
    assert passed is True
    assert reasons == []


# ---------- is_vacancy: мусор не должен доезжать до платного ИИ ----------


def test_spam_rejected_before_ai():
    passed, reasons = passes_hard_rules(_vacancy(is_vacancy=False), Criteria())
    assert passed is False
    assert "не вакансия" in reasons[0]


def test_unenriched_vacancy_not_rejected():
    # is_vacancy=None -> обогащения не было, это не повод отказывать
    passed, _ = passes_hard_rules(_vacancy(is_vacancy=None), Criteria())
    assert passed is True


# ---------- формат работы ----------


def test_office_rejected_when_only_remote_wanted():
    passed, reasons = passes_hard_rules(
        _vacancy(work_format=WorkFormat.OFFICE),
        Criteria(work_formats=[WorkFormat.REMOTE]),
    )
    assert passed is False
    assert "формат office" in reasons[0]


def test_remote_passes():
    passed, _ = passes_hard_rules(
        _vacancy(work_format=WorkFormat.REMOTE),
        Criteria(work_formats=[WorkFormat.REMOTE]),
    )
    assert passed is True


# ---------- зарплата: неизвестно != мало ----------


def test_unknown_salary_is_not_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(salary=None), Criteria(min_salary=200_000)
    )
    assert passed is True, "нераспознанная зарплата не повод отсекать"
    assert reasons == []


def test_salary_with_raw_but_no_numbers_is_not_rejected():
    passed, _ = passes_hard_rules(
        _vacancy(salary=Salary(raw="по договорённости", max_value=None)),
        Criteria(min_salary=200_000),
    )
    assert passed is True


def test_salary_below_threshold_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(salary=Salary(raw="100-150к", min_value=100_000, max_value=150_000)),
        Criteria(min_salary=200_000),
    )
    assert passed is False
    assert "ниже порога" in reasons[0]


def test_salary_range_judged_by_upper_bound():
    # 160-210к при пороге 200к проходит: договориться на 210 реально
    passed, _ = passes_hard_rules(
        _vacancy(salary=Salary(raw="160-210к", min_value=160_000, max_value=210_000)),
        Criteria(min_salary=200_000),
    )
    assert passed is True


# ---------- грейд ----------


def test_unknown_grade_is_not_rejected():
    passed, _ = passes_hard_rules(
        _vacancy(grade=Grade.UNKNOWN), Criteria(grades=[Grade.SENIOR])
    )
    assert passed is True, "неизвестный грейд пропускаем на ИИ"


def test_unknown_work_format_is_not_rejected():
    # тот же принцип, что и с грейдом/зарплатой: не распознали != не подходит.
    # На замере этот баг молча отсекал 14 постов из 300.
    passed, _ = passes_hard_rules(
        _vacancy(work_format=WorkFormat.UNKNOWN),
        Criteria(work_formats=[WorkFormat.REMOTE]),
    )
    assert passed is True, "нераспознанный формат пропускаем на ИИ"


def test_wrong_grade_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(grade=Grade.JUNIOR), Criteria(grades=[Grade.SENIOR, Grade.MIDDLE])
    )
    assert passed is False
    assert "грейд junior" in reasons[0]


# ---------- стек ----------


def test_stack_intersection_passes():
    passed, _ = passes_hard_rules(
        _vacancy(stack=["Python", "Docker"]), Criteria(stack_include=["Python"])
    )
    assert passed is True


def test_stack_no_intersection_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(stack=["Java", "Kotlin"]), Criteria(stack_include=["Python"])
    )
    assert passed is False
    assert "не пересёкся" in reasons[0]


def test_empty_stack_is_not_rejected():
    passed, _ = passes_hard_rules(_vacancy(stack=[]), Criteria(stack_include=["Python"]))
    assert passed is True, "стек не распознан — неизвестность, а не отказ"


def test_stack_entirely_excluded_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(stack=["Java"]), Criteria(stack_exclude=["Java"])
    )
    assert passed is False
    assert "целиком из стоп-технологий" in reasons[0]


def test_one_excluded_tech_among_wanted_passes():
    # чужая технология рядом с нужными — норма, а не повод отказать
    passed, _ = passes_hard_rules(
        _vacancy(stack=["Python", "Java"]),
        Criteria(stack_include=["Python"], stack_exclude=["Java"]),
    )
    assert passed is True


def test_all_reasons_collected_not_just_first():
    passed, reasons = passes_hard_rules(
        _vacancy(
            work_format=WorkFormat.OFFICE,
            grade=Grade.JUNIOR,
            stack=["Java"],
            salary=Salary(raw="100к", min_value=100_000, max_value=100_000),
        ),
        Criteria(
            work_formats=[WorkFormat.REMOTE],
            grades=[Grade.SENIOR],
            stack_include=["Python"],
            min_salary=200_000,
        ),
    )
    assert passed is False
    assert len(reasons) == 4, f"должны быть все причины, а не первая: {reasons}"


# ---------- оркестратор: ИИ не зовётся на отсеянных ----------


def test_ai_not_called_when_rules_reject(monkeypatch):
    import src.filters.filter as filter_module

    called = []
    monkeypatch.setattr(
        filter_module, "assess_relevance", lambda v, c: called.append(1)
    )

    result = filter_module.filter_vacancy(
        _vacancy(work_format=WorkFormat.OFFICE), Criteria(work_formats=[WorkFormat.REMOTE])
    )
    assert result.passed is False
    assert called == [], "ИИ звать нельзя — правила уже отсекли, это платный вызов"
    assert result.score is None


def test_ai_called_when_rules_pass(monkeypatch):
    import src.filters.filter as filter_module
    from src.filters.relevance import RelevanceResult, Score

    monkeypatch.setattr(
        filter_module,
        "assess_relevance",
        lambda v, c: RelevanceResult(score=Score.HIGH, reasoning="стек совпал"),
    )
    result = filter_module.filter_vacancy(_vacancy(work_format=WorkFormat.REMOTE), Criteria())
    assert result.passed is True
    assert result.score is Score.HIGH
    assert result.reasoning == "стек совпал"


def test_low_score_does_not_pass(monkeypatch):
    import src.filters.filter as filter_module
    from src.filters.relevance import RelevanceResult, Score

    monkeypatch.setattr(
        filter_module,
        "assess_relevance",
        lambda v, c: RelevanceResult(score=Score.LOW, reasoning="по сути мимо"),
    )
    result = filter_module.filter_vacancy(_vacancy(), Criteria())
    assert result.passed is False
    assert result.score is Score.LOW


def test_ai_failure_does_not_lose_vacancy(monkeypatch):
    import src.filters.filter as filter_module

    monkeypatch.setattr(filter_module, "assess_relevance", lambda v, c: None)
    result = filter_module.filter_vacancy(_vacancy(), Criteria())
    assert result.passed is True, "правила прошла — не теряем из-за ошибки API"
    assert result.score is None
