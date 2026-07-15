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


def test_missing_tool_does_not_reject():
    """
    Инструменты правилами НЕ отсекают: нет Docker в резюме, а в вакансии нужен —
    выучить неделя. Раньше такая вакансия выкидывалась, владелец поймал это.
    """
    passed, _ = passes_hard_rules(
        _vacancy(stack=["Python", "Docker", "Kubernetes"]),
        Criteria(stack_include=["Python", "Playwright"], languages=["Python"]),
    )
    assert passed is True, "незнакомый инструмент — не повод отказать"


def test_empty_stack_is_not_rejected():
    passed, _ = passes_hard_rules(_vacancy(stack=[]), Criteria(languages=["Python"]))
    assert passed is True, "стек не распознан — неизвестность, а не отказ"


# ---------- язык: жёсткий критерий ----------


def test_foreign_language_rejected():
    # реальный случай: Java-вакансии доезжали до бота через пересечение по Selenium
    passed, reasons = passes_hard_rules(
        _vacancy(stack=["Java", "Selenium", "API"]), Criteria(languages=["Python"])
    )
    assert passed is False
    assert "язык Java" in reasons[0]


def test_python_slash_go_passes():
    # "python/go — норм, но если только go — уже нет"
    passed, _ = passes_hard_rules(
        _vacancy(stack=["Python", "Go"]), Criteria(languages=["Python"])
    )
    assert passed is True, "Python есть среди языков вакансии — годится"


def test_only_go_rejected():
    passed, reasons = passes_hard_rules(
        _vacancy(stack=["Go", "Docker"]), Criteria(languages=["Python"])
    )
    assert passed is False
    assert "язык Go" in reasons[0]


def test_vacancy_without_named_language_is_not_rejected():
    # язык не назван -> неизвестность, а не отказ (как с зарплатой)
    passed, _ = passes_hard_rules(
        _vacancy(stack=["Selenium", "Postman"]), Criteria(languages=["Python"])
    )
    assert passed is True


def test_empty_languages_criterion_passes_everything():
    passed, _ = passes_hard_rules(_vacancy(stack=["Java"]), Criteria())
    assert passed is True, "пустой критерий = не применяется"


def test_criteria_rejects_tool_as_language():
    with pytest.raises(ValueError, match="не язык"):
        Criteria(languages=["Docker"])


def test_language_aliases_normalized():
    assert Criteria(languages=["golang", "c#"]).languages == ["Go", "C#"]


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
            languages=["Python"],
            min_salary=200_000,
        ),
    )
    assert passed is False
    assert len(reasons) == 4, f"должны быть все причины, а не первая: {reasons}"


# ---------- оркестратор: ИИ не зовётся на отсеянных ----------


async def test_ai_not_called_when_rules_reject(monkeypatch):
    import src.filters.filter as filter_module

    called = []

    async def spy(vacancy, criteria):
        called.append(1)

    monkeypatch.setattr(filter_module, "assess_relevance", spy)

    result = await filter_module.filter_vacancy(
        _vacancy(work_format=WorkFormat.OFFICE), Criteria(work_formats=[WorkFormat.REMOTE])
    )
    assert result.passed is False
    assert called == [], "ИИ звать нельзя — правила уже отсекли, это платный вызов"
    assert result.score is None


async def test_ai_called_when_rules_pass(monkeypatch):
    import src.filters.filter as filter_module
    from src.filters.relevance import RelevanceResult, Score

    async def fake(vacancy, criteria):
        return RelevanceResult(score=Score.HIGH, reasoning="стек совпал")

    monkeypatch.setattr(filter_module, "assess_relevance", fake)
    result = await filter_module.filter_vacancy(
        _vacancy(work_format=WorkFormat.REMOTE), Criteria()
    )
    assert result.passed is True
    assert result.score is Score.HIGH
    assert result.reasoning == "стек совпал"


async def test_low_score_does_not_pass(monkeypatch):
    import src.filters.filter as filter_module
    from src.filters.relevance import RelevanceResult, Score

    async def fake(vacancy, criteria):
        return RelevanceResult(score=Score.LOW, reasoning="по сути мимо")

    monkeypatch.setattr(filter_module, "assess_relevance", fake)
    result = await filter_module.filter_vacancy(_vacancy(), Criteria())
    assert result.passed is False
    assert result.score is Score.LOW


async def test_no_data_score_still_passes(monkeypatch):
    """
    Пост-заглушка ("должность + ссылка") — это "не знаем", а не "не подходит".
    За ссылкой может оказаться отличная вакансия, поэтому в выдачу идёт.
    """
    import src.filters.filter as filter_module
    from src.filters.relevance import RelevanceResult, Score

    async def fake(vacancy, criteria):
        return RelevanceResult(score=Score.NO_DATA, reasoning="в посте только ссылка")

    monkeypatch.setattr(filter_module, "assess_relevance", fake)
    result = await filter_module.filter_vacancy(_vacancy(), Criteria())

    assert result.passed is True, "заглушку не теряем — решать по ссылке"
    assert result.score is Score.NO_DATA


def test_no_data_badge_differs_from_medium():
    """В выдаче заглушку надо отличать от «спорно» с одного взгляда."""
    from src.delivery.telegram_bot import _SCORE_BADGE

    assert _SCORE_BADGE["no_data"] != _SCORE_BADGE["medium"]
    assert set(_SCORE_BADGE) == {"high", "medium", "low", "no_data"}


async def test_ai_failure_does_not_lose_vacancy(monkeypatch):
    import src.filters.filter as filter_module

    async def fake(vacancy, criteria):
        return None

    monkeypatch.setattr(filter_module, "assess_relevance", fake)
    result = await filter_module.filter_vacancy(_vacancy(), Criteria())
    assert result.passed is True, "правила прошла — не теряем из-за ошибки API"
    assert result.score is None
