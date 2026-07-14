"""
Хранилище (SQLite) — дедупликация и история доставленного.

Дедуп ДВУХУРОВНЕВЫЙ, уровни решают разные задачи:

  1. seen_posts (channel + message_id) — "этот пост мы уже смотрели".
     Точный, бесплатный, срабатывает ДО любого вызова ИИ. Это он превращает
     повторный прогон из 50 минут в секунды: обрабатываем только новое.

  2. vacancies.content_hash — "эту вакансию мы уже присылали".
     Ловит репосты и кросс-постинг между каналами (один и тот же текст).

Почему ключ второго уровня — хэш текста, а не (компания + должность), как
предполагалось изначально: замерено на 300 реальных постах.
     хэш норм. текста : схлопнул 20 постов, потеряно уникальных вакансий 0
     title + contact  : схлопнул 83, ПОТЕРЯНО 63
     только title     : схлопнул 103, ПОТЕРЯНО 83
Один HR постит РАЗНЫЕ вакансии под одинаковым названием ("QA Engineer" — 15
разных вакансий), поэтому ключ по названию сливает несвязанное. Асимметрия
важна: прислать дубль — досадно, потерять вакансию — провал. Хэш строже.
"""

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "vacancies.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_posts (
    channel      TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

CREATE TABLE IF NOT EXISTS vacancies (
    content_hash TEXT PRIMARY KEY,
    channel      TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    title        TEXT,
    company      TEXT,
    grade        TEXT,
    work_format  TEXT,
    salary_min   INTEGER,
    salary_max   INTEGER,
    contact      TEXT,
    score        TEXT,
    reasoning    TEXT,
    link         TEXT,
    posted_at    TEXT,
    delivered_at TEXT
);
"""

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def content_hash(raw_text: str) -> str:
    """
    Хэш от НОРМАЛИЗОВАННОГО текста: репост часто отличается эмодзи, markdown
    или лишними пробелами — по сырому тексту такие дубли не поймались бы.
    """
    text = raw_text.replace("*", "").lower()
    text = _PUNCT.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------- уровень 1: пост уже смотрели ----------


def seen_message_ids(conn: sqlite3.Connection, channel: str) -> set[int]:
    """Одним запросом на канал, а не по одному на пост."""
    rows = conn.execute(
        "SELECT message_id FROM seen_posts WHERE channel = ?", (channel,)
    ).fetchall()
    return {row["message_id"] for row in rows}


def mark_seen(conn: sqlite3.Connection, channel: str, message_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts (channel, message_id, processed_at) "
        "VALUES (?, ?, ?)",
        (channel, message_id, datetime.now().isoformat(timespec="seconds")),
    )


# ---------- уровень 2: вакансию уже присылали ----------


def exists(conn: sqlite3.Connection, hash_value: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM vacancies WHERE content_hash = ?", (hash_value,)
    ).fetchone()
    return row is not None


def save_vacancy(
    conn: sqlite3.Connection,
    *,
    hash_value: str,
    channel: str,
    message_id: int,
    vacancy,
    score: str | None,
    reasoning: str | None,
    link: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO vacancies (content_hash, channel, message_id, title, "
        "company, grade, work_format, salary_min, salary_max, contact, score, "
        "reasoning, link, posted_at, delivered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            hash_value,
            channel,
            message_id,
            vacancy.title,
            vacancy.company,
            vacancy.grade.value if vacancy.grade else None,
            vacancy.work_format.value if vacancy.work_format else None,
            vacancy.salary.min_value if vacancy.salary else None,
            vacancy.salary.max_value if vacancy.salary else None,
            vacancy.contact,
            score,
            reasoning,
            link,
            vacancy.posted_at.isoformat(),
        ),
    )


def mark_delivered(conn: sqlite3.Connection, hash_value: str) -> None:
    conn.execute(
        "UPDATE vacancies SET delivered_at = ? WHERE content_hash = ?",
        (datetime.now().isoformat(timespec="seconds"), hash_value),
    )
