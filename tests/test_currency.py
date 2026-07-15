"""
Валюта: 10% постов дают вилку в долларах (замер 2026-07-15: 40 из 390).

Цена ошибки односторонняя и высокая: сравнить "2500 USD" с порогом "230000 RUB"
как голые числа — значит молча выкинуть вакансию на ~200к. Поэтому конвертация
живёт свойством модели, а не в вызывающем коде.
"""

from datetime import datetime

import pytest

from config.currency import RATES_TO_RUB, to_rub
from src.filters.criteria import Criteria
from src.filters.rules import passes_hard_rules
from src.models.vacancy import Salary, Vacancy, WorkFormat

NOW = datetime(2026, 1, 1)


def _vacancy(min_value, max_value, currency):
    return Vacancy(
        raw_text="текст",
        posted_at=NOW,
        is_vacancy=True,
        title="QA",
        stack=["Python"],
        work_format=WorkFormat.REMOTE,
        salary=Salary(
            min_value=min_value, max_value=max_value, currency=currency, raw="вилка"
        ),
    )


# ---------- пересчёт ----------


def test_rubles_pass_through():
    assert to_rub(230000, "RUB") == 230000


def test_no_currency_means_rubles():
    """В этих каналах рубль по умолчанию — иначе половина вилок стала бы None."""
    assert to_rub(230000, None) == 230000


def test_dollars_converted():
    assert to_rub(2500, "USD") == int(2500 * RATES_TO_RUB["USD"])


def test_case_and_spaces_do_not_break_it():
    assert to_rub(1000, " usd ") == to_rub(1000, "USD")


def test_unknown_currency_is_none_not_raw():
    """
    Вернуть 3000 тугриков как "3000 рублей" — соврать правилу, и оно выкинет
    вакансию. None честнее: неизвестное правилами не режем, разберётся ИИ.
    """
    assert to_rub(3000, "MNT") is None


def test_none_stays_none():
    assert to_rub(None, "USD") is None


# ---------- свойства модели ----------


def test_model_exposes_rubles():
    salary = Salary(min_value=2500, max_value=3700, currency="USD", raw="2500-3700$")

    assert salary.min_rub == int(2500 * RATES_TO_RUB["USD"])
    assert salary.max_rub == int(3700 * RATES_TO_RUB["USD"])
    assert salary.min_value == 2500, "исходная запись не должна подменяться"


# ---------- правило ----------


def test_dollar_vacancy_above_threshold_passes():
    """Главный тест. $3000 ~ 240к — это ВЫШЕ порога 230к, вакансию терять нельзя."""
    criteria = Criteria(min_salary=230000, languages=["Python"])

    passed, reasons = passes_hard_rules(_vacancy(3000, 3000, "USD"), criteria)

    assert passed is True, f"вакансия на ~240к отсеяна: {reasons}"


def test_dollar_vacancy_below_threshold_rejected():
    """Обратная сторона: $1000 ~ 80к, это честно ниже порога."""
    criteria = Criteria(min_salary=230000, languages=["Python"])

    passed, reasons = passes_hard_rules(_vacancy(1000, 1000, "USD"), criteria)

    assert passed is False
    assert "ниже порога" in reasons[0]


def test_reason_text_is_in_rubles():
    """В причине отказа рубли: '1к ниже порога 230к' было бы бессмыслицей."""
    criteria = Criteria(min_salary=230000, languages=["Python"])

    _, reasons = passes_hard_rules(_vacancy(1000, 1000, "USD"), criteria)

    assert f"{int(1000 * RATES_TO_RUB['USD']) // 1000}к" in reasons[0]


def test_unknown_currency_is_not_rejected():
    """Неизвестная валюта = неизвестная зарплата, а неизвестное не отсекаем."""
    criteria = Criteria(min_salary=230000, languages=["Python"])

    passed, _ = passes_hard_rules(_vacancy(3000, 3000, "MNT"), criteria)

    assert passed is True


@pytest.mark.parametrize("currency", ["RUB", "USD", "EUR"])
def test_rates_cover_currencies_the_ai_may_return(currency):
    """Схема разрешает ИИ вернуть эти коды — курс для них обязан быть."""
    assert currency in RATES_TO_RUB
