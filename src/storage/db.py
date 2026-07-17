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
import json
import re
import sqlite3
from datetime import datetime, timezone
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
    -- полный текст поста. Без него отладка и переоценка лезут в Telegram за
    -- исходником: нужен .session, а он живёт в одном месте. С ним БД —
    -- самодостаточный артефакт: скачал файл и воспроизвёл что угодно офлайн.
    raw_text     TEXT,
    grade        TEXT,
    work_format  TEXT,
    salary_min   INTEGER,
    salary_max   INTEGER,
    -- валюта поста: без неё постфактум не понять, 2500 это рубли или доллары
    salary_currency TEXT,
    contact      TEXT,
    score        TEXT,
    reasoning    TEXT,
    link         TEXT,
    posted_at    TEXT,
    message      TEXT,
    delivered_at TEXT,
    seen_at      TEXT,
    -- РЕШЕНИЕ ПОЛЬЗОВАТЕЛЯ: applied / rejected / NULL.
    -- Не путать с seen_at: тот значит «бот показал», а это «я разобрался».
    -- Без него архив заставляет каждый раз перечитывать одни и те же вакансии.
    decision     TEXT,
    decided_at   TEXT,
    -- под какими критериями вакансию оценивали. Без этого не отличить
    -- протухшую запись от свежей, и переоценка платит ИИ за все подряд.
    criteria_hash TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Колонки, доехавшие позже создания таблицы. CREATE TABLE IF NOT EXISTS их не
# добавит — существующую БД надо мигрировать явно.
_LATE_COLUMNS = {
    "message": "TEXT",
    "seen_at": "TEXT",
    "criteria_hash": "TEXT",
    "salary_currency": "TEXT",
    "raw_text": "TEXT",
    "decision": "TEXT",
    "decided_at": "TEXT",
}

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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(vacancies)")}
    added = set()
    for name, sql_type in _LATE_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE vacancies ADD COLUMN {name} {sql_type}")
            added.add(name)

    if "seen_at" in added:
        # Вакансии, доставленные в старой push-модели, пользователь уже видел.
        # Без этого при переходе на дайджест они разом всплыли бы как "новые".
        conn.execute(
            "UPDATE vacancies SET seen_at = delivered_at WHERE delivered_at IS NOT NULL"
        )
    conn.commit()


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


def is_delivered(conn: sqlite3.Connection, hash_value: str) -> bool:
    """
    Именно ДОСТАВЛЕНА, а не просто есть в базе. Разница принципиальна: запись
    без delivered_at — это очередь на досылку (DRY_RUN, упавшая отправка), а
    не повод считать вакансию отработанной.
    """
    row = conn.execute(
        "SELECT 1 FROM vacancies WHERE content_hash = ? AND delivered_at IS NOT NULL",
        (hash_value,),
    ).fetchone()
    return row is not None


def exists(conn: sqlite3.Connection, hash_value: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM vacancies WHERE content_hash = ?", (hash_value,)
    ).fetchone()
    return row is not None


def pending_deliveries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Сохранённые, но не отправленные — досылаем на следующем проходе.

    Фильтра по message тут БЫТЬ НЕ ДОЛЖНО: записи, сделанные до появления этой
    колонки, лежат с message=NULL, а посты у них уже помечены виденными. Отсеяв
    их здесь, мы потеряли бы вакансии навсегда — ровно это и случилось на живом
    прогоне. Текст для таких строк собирается из полей (format_message_from_row).
    """
    return conn.execute(
        "SELECT * FROM vacancies WHERE delivered_at IS NULL ORDER BY posted_at"
    ).fetchall()


def criteria_fingerprint(criteria) -> str:
    """
    Отпечаток критериев, под которыми оценивали вакансию.

    Берём только поля, влияющие на отбор: инструменты мягкие, на решение не
    влияют — их правка не должна тянуть за собой платную переоценку.
    """
    payload = json.dumps(
        {
            "languages": sorted(criteria.languages),
            "min_salary": criteria.min_salary,
            "work_formats": sorted(f.value for f in criteria.work_formats),
            "grades": sorted(g.value for g in criteria.grades),
            "stack_exclude": sorted(criteria.stack_exclude),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
    message: str,
    criteria_hash: str | None = None,
) -> None:
    """
    Сохраняем ВСЕГДА с delivered_at=NULL — это постановка в очередь.
    Готовый текст сообщения кладём сюда же, чтобы досылка не требовала
    заново гонять пост через ИИ.
    """
    conn.execute(
        "INSERT OR IGNORE INTO vacancies (content_hash, channel, message_id, title, "
        "company, raw_text, grade, work_format, salary_min, salary_max, "
        "salary_currency, contact, score, reasoning, link, posted_at, message, "
        "delivered_at, criteria_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
        (
            hash_value,
            channel,
            message_id,
            vacancy.title,
            vacancy.company,
            vacancy.raw_text,
            vacancy.grade.value if vacancy.grade else None,
            vacancy.work_format.value if vacancy.work_format else None,
            vacancy.salary.min_value if vacancy.salary else None,
            vacancy.salary.max_value if vacancy.salary else None,
            vacancy.salary.currency if vacancy.salary else None,
            vacancy.contact,
            score,
            reasoning,
            link,
            vacancy.posted_at.isoformat(),
            message,
            criteria_hash,
        ),
    )


def reassess_vacancy(
    conn: sqlite3.Connection,
    *,
    hash_value: str,
    vacancy,
    score: str | None,
    reasoning: str | None,
    message: str,
    criteria_hash: str,
) -> None:
    """
    Переоценка: обновляем вердикт и разобранные поля, seen_at не трогаем.

    raw_text дописываем заодно: у старых записей его нет, и переоценка — это
    ровно тот момент, когда исходник уже в руках. Так они вылечиваются сами и
    второй раз в Telegram идти не придётся.
    """
    conn.execute(
        "UPDATE vacancies SET title=?, company=?, raw_text=?, grade=?, work_format=?, "
        "salary_min=?, salary_max=?, salary_currency=?, contact=?, score=?, "
        "reasoning=?, message=?, "
        "criteria_hash=? WHERE content_hash=?",
        (
            vacancy.title,
            vacancy.company,
            vacancy.raw_text,
            vacancy.grade.value if vacancy.grade else None,
            vacancy.work_format.value if vacancy.work_format else None,
            vacancy.salary.min_value if vacancy.salary else None,
            vacancy.salary.max_value if vacancy.salary else None,
            vacancy.salary.currency if vacancy.salary else None,
            vacancy.contact,
            score,
            reasoning,
            message,
            criteria_hash,
            hash_value,
        ),
    )


def drop_vacancy(conn: sqlite3.Connection, hash_value: str) -> None:
    """Вакансия больше не проходит критерии — из архива её убираем."""
    conn.execute("DELETE FROM vacancies WHERE content_hash = ?", (hash_value,))


def stale_vacancies(
    conn: sqlite3.Connection, criteria_hash: str
) -> list[sqlite3.Row]:
    """Оценённые под другими критериями (или до появления отпечатка)."""
    return conn.execute(
        "SELECT * FROM vacancies WHERE criteria_hash IS NULL OR criteria_hash != ? "
        "ORDER BY posted_at",
        (criteria_hash,),
    ).fetchall()


def mark_delivered(conn: sqlite3.Connection, hash_value: str) -> None:
    conn.execute(
        "UPDATE vacancies SET delivered_at = ? WHERE content_hash = ?",
        (datetime.now().isoformat(timespec="seconds"), hash_value),
    )


# ---------- настройки (key-value) ----------


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_criteria(conn: sqlite3.Connection):
    """None — резюме ещё не загружали, надо гнать онбординг."""
    from src.filters.criteria import Criteria

    raw = get_setting(conn, "criteria")
    return Criteria.model_validate_json(raw) if raw else None


def save_criteria(conn: sqlite3.Connection, criteria) -> None:
    set_setting(conn, "criteria", criteria.model_dump_json())


def is_fetch_enabled(conn: sqlite3.Connection) -> bool:
    """По умолчанию включено — пока пользователь явно не поставил на паузу."""
    return get_setting(conn, "fetch_enabled") != "0"


def set_fetch_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    set_setting(conn, "fetch_enabled", "1" if enabled else "0")


def get_last_fetch_at(conn: sqlite3.Connection) -> datetime | None:
    """
    Когда последний раз ходили по каналам. Всегда aware.

    Записи до появления пояса наивные — трактуем их как UTC: пишет их сервер,
    а он живёт в UTC. Ошибиться тут значит показать пользователю время, по
    которому он решит, что бот мёртв.
    """
    raw = get_setting(conn, "last_fetch_at")
    if not raw:
        return None
    at = datetime.fromisoformat(raw)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def get_last_fetch_found(conn: sqlite3.Connection) -> int | None:
    raw = get_setting(conn, "last_fetch_found")
    return int(raw) if raw is not None else None


def touch_last_fetch(conn: sqlite3.Connection, found: int = 0) -> None:
    """
    Отметка «обход был». Пишем В UTC С ПОЯСОМ: сервер в UTC, владелец в Москве,
    и наивное время показало бы ему 04:02 вместо 07:02 — то есть выглядело бы
    как пропущенный прогон.

    found — сколько НОВЫХ вакансий этот обход добавил. Ноль тоже ценен: он
    отличает «сходил и ничего не нашёл» от «не ходил вовсе», а по тишине в
    боте эти два случая неразличимы.
    """
    set_setting(
        conn, "last_fetch_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    set_setting(conn, "last_fetch_found", str(found))


# ---------- выборки для бота (всегда из БД, без сети) ----------


def count_unseen(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM vacancies WHERE seen_at IS NULL"
    ).fetchone()["n"]


# SELECT rowid, * — а не просто *: rowid нужен как короткий ключ для
# callback_data кнопок «Откликнулся/Не буду». content_hash туда не влезает —
# он 64 символа, а лимит callback_data ровно 64 байта вместе с префиксом.


def unseen_vacancies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rowid, * FROM vacancies WHERE seen_at IS NULL ORDER BY posted_at DESC"
    ).fetchall()


def vacancies_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    """Срез по дате поста — для кнопок 'за сегодня/3 дня/неделю'."""
    return conn.execute(
        "SELECT rowid, * FROM vacancies WHERE posted_at >= ? ORDER BY posted_at DESC",
        (since.isoformat(),),
    ).fetchall()


# ---------- решение пользователя по вакансии ----------

APPLIED = "applied"
REJECTED = "rejected"


def set_decision(conn: sqlite3.Connection, rowid: int, decision: str | None) -> None:
    """
    Отметить «откликнулся» / «не буду» / снять отметку (decision=None).

    seen_at не трогаем: это разные вещи. seen_at — «бот показал», decision —
    «я разобрался». Первое ставится автоматически при показе, второе только
    руками.
    """
    conn.execute(
        "UPDATE vacancies SET decision = ?, decided_at = ? WHERE rowid = ?",
        (
            decision,
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if decision
            else None,
            rowid,
        ),
    )
    conn.commit()


def get_vacancy(conn: sqlite3.Connection, rowid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT rowid, * FROM vacancies WHERE rowid = ?", (rowid,)
    ).fetchone()


def _archive_where(
    score: str | None, undecided: bool, before: str | None
) -> tuple[str, list]:
    """Условия архива в одном месте: иначе count и выборка разъедутся."""
    conditions, params = [], []
    if score is not None:
        conditions.append("score = ?")
        params.append(score)
    if undecided:
        conditions.append("decision IS NULL")
    if before:
        conditions.append("posted_at < ?")
        params.append(before)
    return (f" WHERE {' AND '.join(conditions)}" if conditions else ""), params


def count_archive(
    conn: sqlite3.Connection,
    score: str | None = None,
    undecided: bool = False,
    before: str | None = None,
) -> int:
    where, params = _archive_where(score, undecided, before)
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM vacancies{where}", params
    ).fetchone()["n"]


def vacancies_page(
    conn: sqlite3.Connection,
    score: str | None = None,
    undecided: bool = False,
    limit: int = 10,
    before: str | None = None,
) -> list[sqlite3.Row]:
    """
    Архив: всё, что есть, независимо от seen_at и возраста поста.
    Без этого показанная вакансия старше недели недостижима из бота — в БД
    лежит, а показать нечем.

    undecided=True — только те, по которым решения ещё нет. Ради этого фильтра
    всё и затевалось: без него архив заставляет перечитывать уже разобранное.

    КУРСОР (before), а не OFFSET. Разница принципиальна именно для undecided:
    набор тает по мере разметки, и offset по нему промахивается. Разметил
    первую десятку -> следующие сдвинулись на её место -> offset=10 их
    ПЕРЕПРЫГИВАЕТ, и ты их никогда не увидишь. А когда offset уезжает за
    конец, приходит «Ничего нет» на ровном месте (поймано на живых данных:
    кнопка вела на 40, а без решения осталось 32).

    Курсор — posted_at последней показанной. Проверено на боевой базе: NULL
    нет, формат у всех один (+00:00), дублей нет — строковое сравнение честно
    повторяет порядок сортировки.
    """
    where, params = _archive_where(score, undecided, before)
    return conn.execute(
        f"SELECT rowid, * FROM vacancies{where} ORDER BY posted_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


def mark_seen_vacancy(conn: sqlite3.Connection, hash_value: str) -> None:
    conn.execute(
        "UPDATE vacancies SET seen_at = ? WHERE content_hash = ? AND seen_at IS NULL",
        (datetime.now().isoformat(timespec="seconds"), hash_value),
    )
