"""
Тесты мёржа ИИ-ответа в модель. Без вызовов API — _merge чистая функция,
а правило "ИИ не трогает то, что взяли регулярки" проверяется именно тут.
"""

from datetime import datetime

from src.enrichment.enricher import _failed, _merge
from src.enrichment.schema import EnrichedSalary, EnrichmentResult
from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat

NOW = datetime(2026, 1, 1)


def _vacancy(**kwargs) -> Vacancy:
    return Vacancy(raw_text="текст поста", posted_at=NOW, **kwargs)


def _result(**kwargs) -> EnrichmentResult:
    defaults = dict(
        is_vacancy=True,
        title=None,
        company=None,
        grade=Grade.UNKNOWN,
        salary=None,
        salary_alternatives=[],
    )
    return EnrichmentResult(**{**defaults, **kwargs})


# ---------- ИИ не трогает уверенно взятое регулярками ----------


def test_ai_does_not_overwrite_regex_title():
    vacancy = _vacancy(title="QA Automation Engineer")
    merged = _merge(vacancy, _result(title="Совсем другая должность"))
    assert merged.title == "QA Automation Engineer"


def test_ai_does_not_overwrite_regex_grade():
    vacancy = _vacancy(grade=Grade.MIDDLE)
    merged = _merge(vacancy, _result(grade=Grade.SENIOR))
    assert merged.grade is Grade.MIDDLE


def test_ai_does_not_touch_work_format_contact_hashtags():
    vacancy = _vacancy(
        work_format=WorkFormat.REMOTE,
        contact="@hr_user",
        hashtags=["#вакансия", "#QA"],
    )
    merged = _merge(vacancy, _result())
    assert merged.work_format is WorkFormat.REMOTE
    assert merged.contact == "@hr_user"
    assert merged.hashtags == ["#вакансия", "#QA"]


def test_merge_does_not_mutate_input():
    vacancy = _vacancy(title=None)
    _merge(vacancy, _result(title="QA Engineer"))
    assert vacancy.title is None, "исходный объект должен остаться нетронутым"


# ---------- ИИ заполняет пустое ----------


def test_ai_fills_empty_title():
    merged = _merge(_vacancy(title=None), _result(title="AQA Engineer"))
    assert merged.title == "AQA Engineer"


def test_ai_fills_unknown_grade():
    # регулярки пишут UNKNOWN когда не нашли — для грейда это "пусто"
    merged = _merge(_vacancy(grade=Grade.UNKNOWN), _result(grade=Grade.SENIOR))
    assert merged.grade is Grade.SENIOR


def test_ai_fills_company_regex_never_takes():
    merged = _merge(_vacancy(), _result(company="Bell Integrator"))
    assert merged.company == "Bell Integrator"


# ---------- is_vacancy считается всегда ----------


def test_is_vacancy_marks_spam():
    merged = _merge(_vacancy(), _result(is_vacancy=False))
    assert merged.is_vacancy is False


def test_is_vacancy_always_set_even_when_true():
    merged = _merge(_vacancy(), _result(is_vacancy=True))
    assert merged.is_vacancy is True


# ---------- зарплата: ИИ нормализует всегда, raw от регулярок в приоритете ----------


def test_ai_normalizes_salary_and_keeps_regex_raw():
    vacancy = _vacancy(salary=Salary(raw="160-210к", min_value=160_000))
    merged = _merge(
        vacancy,
        _result(
            salary=EnrichedSalary(
                min_value=160_000,
                max_value=210_000,
                currency="RUB",
                gross=False,
                period="month",
                raw="160-210к на руки",
            )
        ),
    )
    assert merged.salary.max_value == 210_000
    assert merged.salary.currency == "RUB"
    assert merged.salary.period == "month"
    assert merged.salary.raw == "160-210к", "raw от регулярок точнее — он буквальный"


def test_ai_salary_used_when_regex_found_nothing():
    # тот самый "80 тыс", который регулярки не берут
    merged = _merge(
        _vacancy(salary=None),
        _result(
            salary=EnrichedSalary(
                min_value=80_000,
                max_value=120_000,
                currency="RUB",
                gross=False,
                period="month",
                raw="80 тыс - 120 тыс на руки",
            )
        ),
    )
    assert merged.salary.min_value == 80_000
    assert merged.salary.raw == "80 тыс - 120 тыс на руки"


def test_salary_alternatives_collected():
    merged = _merge(
        _vacancy(),
        _result(salary_alternatives=["ИП 150 гросс", "1200-1500 руб/час"]),
    )
    assert merged.salary_alternatives == ["ИП 150 гросс", "1200-1500 руб/час"]


def test_hourly_rate_keeps_period_hour():
    merged = _merge(
        _vacancy(),
        _result(
            salary=EnrichedSalary(
                min_value=1200,
                max_value=1500,
                currency="RUB",
                gross=True,
                period="hour",
                raw="1200 до 1500 руб/час",
            )
        ),
    )
    assert merged.salary.period == "hour"
    assert merged.salary.min_value == 1200, "часовую ставку не переводим в месяц"


# ---------- parse_flags не врут после обогащения ----------


def test_flag_resolved_when_ai_fills_salary():
    vacancy = _vacancy(salary=None, parse_flags=["salary_not_parsed"])
    merged = _merge(
        vacancy,
        _result(
            salary=EnrichedSalary(
                min_value=80_000,
                max_value=80_000,
                currency="RUB",
                gross=False,
                period="month",
                raw="80 тыс",
            )
        ),
    )
    assert "salary_not_parsed" not in merged.parse_flags, "флаг врёт: зарплата есть"
    assert "enriched_by_ai: salary" in merged.parse_flags


def test_flag_resolved_when_ai_fills_grade():
    vacancy = _vacancy(grade=Grade.UNKNOWN, parse_flags=["grade_not_parsed"])
    merged = _merge(vacancy, _result(grade=Grade.SENIOR))
    assert "grade_not_parsed" not in merged.parse_flags
    assert "enriched_by_ai: grade" in merged.parse_flags


def test_flag_stays_when_ai_also_could_not_fill():
    # ИИ тоже не смог — флаг обязан остаться, это честный сигнал
    vacancy = _vacancy(grade=Grade.UNKNOWN, parse_flags=["grade_not_parsed"])
    merged = _merge(vacancy, _result(grade=Grade.UNKNOWN))
    assert "grade_not_parsed" in merged.parse_flags
    assert not any("enriched_by_ai" in flag for flag in merged.parse_flags)


def test_unrelated_flags_survive_resolution():
    vacancy = _vacancy(
        grade=Grade.UNKNOWN,
        parse_flags=["grade_not_parsed", "contact_not_parsed"],
    )
    merged = _merge(vacancy, _result(grade=Grade.LEAD))
    assert "contact_not_parsed" in merged.parse_flags, "чужой флаг трогать нельзя"


def test_salary_flag_not_resolved_when_regex_already_had_salary():
    # регулярки зарплату взяли -> флага не было -> ИИ только нормализовал,
    # это не "заполнение пустого", помечать нечего
    vacancy = _vacancy(salary=Salary(raw="160-210к", min_value=160_000))
    merged = _merge(
        vacancy,
        _result(
            salary=EnrichedSalary(
                min_value=160_000,
                max_value=210_000,
                currency="RUB",
                gross=False,
                period="month",
                raw="160-210к",
            )
        ),
    )
    assert not any("enriched_by_ai: salary" in flag for flag in merged.parse_flags)


# ---------- ошибки не роняют прогон ----------


def test_failed_marks_flag_and_keeps_model():
    vacancy = _vacancy(title="QA Engineer", parse_flags=["grade_not_parsed"])
    failed = _failed(vacancy, "rate_limit: 429")

    assert failed.title == "QA Engineer", "модель должна остаться нетронутой"
    assert failed.is_vacancy is None, "обогащения не было — поле пустое, а не False"
    assert "grade_not_parsed" in failed.parse_flags, "старые флаги сохраняются"
    assert any("enrichment_failed" in flag for flag in failed.parse_flags)


def test_failed_does_not_mutate_input():
    vacancy = _vacancy()
    _failed(vacancy, "connection: timeout")
    assert vacancy.parse_flags == []
