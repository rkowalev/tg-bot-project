"""
Отпечаток критериев: что считается протухшим, а что нет.

Цена ошибки в обе стороны: пропустим протухшее — в архиве останутся вакансии,
отобранные по чужим правилам; сочтём протухшим лишнее — заплатим ИИ за уже
известный ответ.
"""

from datetime import datetime

import pytest

from src.filters.criteria import Criteria
from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat
from src.storage import (
    criteria_fingerprint,
    drop_vacancy,
    reassess_vacancy,
    save_vacancy,
    stale_vacancies,
)
from src.storage.db import connect

NOW = datetime(2026, 1, 1)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _vacancy(title="QA", salary=None):
    return Vacancy(
        raw_text="текст",
        posted_at=NOW,
        is_vacancy=True,
        title=title,
        stack=["Python"],
        work_format=WorkFormat.REMOTE,
        grade=Grade.MIDDLE,
        salary=Salary(min_value=salary, max_value=salary, currency="RUB", raw="x")
        if salary
        else None,
    )


def _save(conn, hash_value, criteria_hash):
    save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=1,
        vacancy=_vacancy(),
        score="high",
        reasoning="ок",
        link="https://t.me/ch/1",
        message="карточка",
        criteria_hash=criteria_hash,
    )


# ---------- что влияет на отпечаток ----------


def test_hard_criteria_change_fingerprint():
    """Язык, зарплата, формат и грейд решают отбор — их правка обязана протухать."""
    base = Criteria(languages=["Python"], min_salary=230000)

    assert criteria_fingerprint(base) != criteria_fingerprint(
        Criteria(languages=["Java"], min_salary=230000)
    )
    assert criteria_fingerprint(base) != criteria_fingerprint(
        Criteria(languages=["Python"], min_salary=200000)
    )
    assert criteria_fingerprint(base) != criteria_fingerprint(
        Criteria(languages=["Python"], min_salary=230000, work_formats=[WorkFormat.REMOTE])
    )


def test_soft_tools_do_not_change_fingerprint():
    """
    Инструменты мягкие: на отбор не влияют, идут в ИИ лишь как контекст.
    Их правка не должна тянуть за собой платную переоценку всего архива.
    """
    without = Criteria(languages=["Python"], stack_include=[])
    with_tools = Criteria(languages=["Python"], stack_include=["Docker", "pytest"])

    assert criteria_fingerprint(without) == criteria_fingerprint(with_tools)


def test_fingerprint_ignores_order():
    """Порядок языков — не смысловая разница, переоценку он вызывать не должен."""
    assert criteria_fingerprint(Criteria(languages=["Python", "Go"])) == (
        criteria_fingerprint(Criteria(languages=["Go", "Python"]))
    )


# ---------- выборка протухших ----------


def test_rows_without_fingerprint_are_stale(conn):
    """Записи до появления колонки — протухшие: чем оценивали, неизвестно."""
    _save(conn, "h1", None)

    assert len(stale_vacancies(conn, criteria_fingerprint(Criteria()))) == 1


def test_rows_with_current_fingerprint_are_fresh(conn):
    current = criteria_fingerprint(Criteria(languages=["Python"]))
    _save(conn, "h1", current)

    assert stale_vacancies(conn, current) == []


def test_rows_with_other_fingerprint_are_stale(conn):
    _save(conn, "h1", criteria_fingerprint(Criteria(languages=["Java"])))

    stale = stale_vacancies(conn, criteria_fingerprint(Criteria(languages=["Python"])))
    assert len(stale) == 1


# ---------- запись результатов переоценки ----------


def test_reassess_updates_verdict_and_keeps_seen(conn):
    """Переоценка меняет вердикт, но не должна воскрешать вакансию как новую."""
    _save(conn, "h1", None)
    conn.execute("UPDATE vacancies SET seen_at = ? WHERE content_hash = 'h1'", ("2026-01-02",))
    current = criteria_fingerprint(Criteria(languages=["Python"]))

    reassess_vacancy(
        conn,
        hash_value="h1",
        vacancy=_vacancy(title="QA новый", salary=300000),
        score="no_data",
        reasoning="в посте только ссылка",
        message="новая карточка",
        criteria_hash=current,
    )

    row = conn.execute("SELECT * FROM vacancies WHERE content_hash='h1'").fetchone()
    assert row["score"] == "no_data"
    assert row["salary_min"] == 300000
    assert row["seen_at"] == "2026-01-02", "показанное не должно всплыть как новое"
    assert stale_vacancies(conn, current) == []


def test_drop_removes_from_archive(conn):
    _save(conn, "h1", None)

    drop_vacancy(conn, "h1")

    assert conn.execute("SELECT COUNT(*) AS n FROM vacancies").fetchone()["n"] == 0
