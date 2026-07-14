"""
Тесты настроек, seen_at и миграции на pull-модель. SQLite в памяти, без сети.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src.filters.criteria import Criteria
from src.models.vacancy import Grade, Vacancy, WorkFormat
from src.storage import db

NOW = datetime(2026, 1, 1)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def _save(conn, hash_value: str, posted_at: datetime = NOW, title: str = "QA"):
    db.save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=1,
        vacancy=Vacancy(raw_text="t", posted_at=posted_at, title=title),
        score="high",
        reasoning="ок",
        link="https://t.me/ch/1",
        message="сводка",
    )
    conn.commit()


# ---------- настройки ----------


def test_fetch_enabled_by_default(conn):
    assert db.is_fetch_enabled(conn) is True, "по умолчанию сбор включён"


def test_fetch_can_be_paused_and_resumed(conn):
    db.set_fetch_enabled(conn, False)
    assert db.is_fetch_enabled(conn) is False
    db.set_fetch_enabled(conn, True)
    assert db.is_fetch_enabled(conn) is True


def test_no_criteria_initially(conn):
    assert db.get_criteria(conn) is None, "без резюме критериев нет — нужен онбординг"


def test_criteria_survive_roundtrip(conn):
    criteria = Criteria(
        work_formats=[WorkFormat.REMOTE],
        min_salary=270_000,
        stack_include=["Python", "pytest"],
        grades=[Grade.SENIOR],
    )
    db.save_criteria(conn, criteria)
    loaded = db.get_criteria(conn)
    assert loaded == criteria, "критерии должны читаться из БД без потерь"


def test_last_fetch_starts_empty_and_updates(conn):
    assert db.get_last_fetch_at(conn) is None
    db.touch_last_fetch(conn)
    assert db.get_last_fetch_at(conn) is not None


# ---------- seen_at: счёт новых ----------


def test_saved_vacancy_is_unseen(conn):
    _save(conn, "h1")
    assert db.count_unseen(conn) == 1


def test_marking_seen_removes_from_new(conn):
    _save(conn, "h1")
    db.mark_seen_vacancy(conn, "h1")
    conn.commit()
    assert db.count_unseen(conn) == 0


def test_marking_seen_twice_keeps_first_timestamp(conn):
    _save(conn, "h1")
    db.mark_seen_vacancy(conn, "h1")
    conn.commit()
    first = conn.execute("SELECT seen_at FROM vacancies").fetchone()["seen_at"]
    db.mark_seen_vacancy(conn, "h1")
    conn.commit()
    second = conn.execute("SELECT seen_at FROM vacancies").fetchone()["seen_at"]
    assert first == second, "повторный показ не должен переписывать время"


# ---------- срезы по датам ----------


def test_period_slice_respects_posted_at(conn):
    _save(conn, "fresh", posted_at=datetime.now(), title="Свежая")
    _save(conn, "old", posted_at=datetime.now() - timedelta(days=10), title="Старая")

    today = db.vacancies_since(conn, datetime.now() - timedelta(days=1))
    week = db.vacancies_since(conn, datetime.now() - timedelta(days=7))

    assert [r["title"] for r in today] == ["Свежая"]
    assert len(week) == 1, "десятидневная вакансия не входит в неделю"


def test_period_slice_does_not_touch_seen(conn):
    # срез — это просмотр, он не должен съедать "новые"
    _save(conn, "h1", posted_at=datetime.now())
    db.vacancies_since(conn, datetime.now() - timedelta(days=1))
    assert db.count_unseen(conn) == 1, "просмотр среза не помечает вакансию показанной"


# ---------- миграция со старой push-модели ----------


def test_migration_marks_delivered_as_seen(tmp_path):
    """
    В БД от Итерации 4 лежат доставленные вакансии. При переходе на дайджест
    они не должны разом всплыть как "новые" — пользователь их уже видел.
    """
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE seen_posts (channel TEXT, message_id INTEGER, processed_at TEXT,
                                 PRIMARY KEY (channel, message_id));
        CREATE TABLE vacancies (
            content_hash TEXT PRIMARY KEY, channel TEXT, message_id INTEGER,
            title TEXT, company TEXT, grade TEXT, work_format TEXT,
            salary_min INTEGER, salary_max INTEGER, contact TEXT, score TEXT,
            reasoning TEXT, link TEXT, posted_at TEXT, delivered_at TEXT);
        """
    )
    raw.execute(
        "INSERT INTO vacancies VALUES ('h1','@ch',1,'Доставленная',NULL,NULL,NULL,"
        "NULL,NULL,NULL,'high',NULL,NULL,'2026-01-01T00:00:00','2026-01-02T10:00:00')"
    )
    raw.execute(
        "INSERT INTO vacancies VALUES ('h2','@ch',2,'Недоставленная',NULL,NULL,NULL,"
        "NULL,NULL,NULL,'high',NULL,NULL,'2026-01-01T00:00:00',NULL)"
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    try:
        assert db.count_unseen(conn) == 1, "доставленная не должна считаться новой"
        assert db.unseen_vacancies(conn)[0]["title"] == "Недоставленная"
    finally:
        conn.close()
