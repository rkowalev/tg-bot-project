"""
Тесты хранилища и дедупликации. Без сети: SQLite в памяти.

Главное, что проверяем — асимметрию дедупа: прислать дубль досадно, но
потерять уникальную вакансию нельзя.
"""

from datetime import datetime

import pytest

from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat
from src.storage import db

NOW = datetime(2026, 1, 1)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def _vacancy(**kwargs) -> Vacancy:
    base = dict(raw_text="текст", posted_at=NOW)
    return Vacancy(**{**base, **kwargs})


# ---------- уровень 1: пост уже видели ----------


def test_unseen_post_is_not_seen(conn):
    assert 42 not in db.seen_message_ids(conn, "@channel")


def test_marked_post_is_seen(conn):
    db.mark_seen(conn, "@channel", 42)
    assert 42 in db.seen_message_ids(conn, "@channel")


def test_seen_is_per_channel(conn):
    db.mark_seen(conn, "@one", 42)
    assert 42 not in db.seen_message_ids(conn, "@two"), "каналы не должны пересекаться"


def test_mark_seen_twice_does_not_crash(conn):
    db.mark_seen(conn, "@channel", 42)
    db.mark_seen(conn, "@channel", 42)
    assert db.seen_message_ids(conn, "@channel") == {42}


# ---------- уровень 2: хэш содержимого ----------


def test_same_text_same_hash():
    assert db.content_hash("Вакансия: QA") == db.content_hash("Вакансия: QA")


def test_hash_ignores_markdown_and_emoji():
    # репост часто отличается только оформлением — такой дубль надо ловить
    a = db.content_hash("**Вакансия:** QA Engineer 🎆")
    b = db.content_hash("Вакансия: QA Engineer")
    assert a == b


def test_hash_ignores_whitespace():
    assert db.content_hash("Вакансия:   QA\n\n\nEngineer") == db.content_hash(
        "Вакансия: QA Engineer"
    )


def test_different_vacancies_have_different_hashes():
    # ключевое: одинаковый title у разных вакансий НЕ должен схлопываться.
    # На замере (компания+должность) терял 63 уникальные вакансии из 300.
    a = db.content_hash("Вакансия: QA Engineer\nКомпания: Ромашка\nЗП: 200к")
    b = db.content_hash("Вакансия: QA Engineer\nКомпания: Лютик\nЗП: 300к")
    assert a != b


# ---------- сохранение и доставка ----------


def test_saved_vacancy_exists(conn):
    hash_value = db.content_hash("текст")
    assert db.exists(conn, hash_value) is False
    db.save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=1,
        vacancy=_vacancy(title="QA"),
        score="high",
        reasoning="ок",
        link="https://t.me/ch/1",
        message="текст сводки",
    )
    assert db.exists(conn, hash_value) is True


def test_saving_duplicate_does_not_crash(conn):
    hash_value = db.content_hash("текст")
    for _ in range(2):
        db.save_vacancy(
            conn,
            hash_value=hash_value,
            channel="@ch",
            message_id=1,
            vacancy=_vacancy(title="QA"),
            score="high",
            reasoning="ок",
            link="https://t.me/ch/1",
            message="текст сводки",
        )
    rows = conn.execute("SELECT COUNT(*) AS n FROM vacancies").fetchone()
    assert rows["n"] == 1, "дубль не должен создавать вторую запись"


def test_full_vacancy_fields_persist(conn):
    hash_value = db.content_hash("текст")
    db.save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=7,
        vacancy=_vacancy(
            title="AQA Python",
            company="Ромашка",
            grade=Grade.SENIOR,
            work_format=WorkFormat.REMOTE,
            salary=Salary(raw="250-330к", min_value=250_000, max_value=330_000),
            contact="@hr",
        ),
        score="high",
        reasoning="стек совпал",
        link="https://t.me/ch/7",
        message="текст сводки",
    )
    row = conn.execute("SELECT * FROM vacancies").fetchone()
    assert row["title"] == "AQA Python"
    assert row["company"] == "Ромашка"
    assert row["grade"] == "senior"
    assert row["work_format"] == "remote"
    assert row["salary_min"] == 250_000
    assert row["salary_max"] == 330_000
    assert row["contact"] == "@hr"
    assert row["score"] == "high"
    assert row["delivered_at"] is None, "до отправки поле пустое"


def test_mark_delivered_sets_timestamp(conn):
    hash_value = db.content_hash("текст")
    db.save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=1,
        vacancy=_vacancy(title="QA"),
        score="high",
        reasoning="ок",
        link="https://t.me/ch/1",
        message="текст сводки",
    )
    db.mark_delivered(conn, hash_value)
    row = conn.execute("SELECT delivered_at FROM vacancies").fetchone()
    assert row["delivered_at"] is not None
