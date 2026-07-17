"""
Решение по вакансии: откликнулся / не буду / ещё не разбирался.

Зачем: архив показывает всё подряд, и одни и те же вакансии приходится
перечитывать. Решение — это отметка «я разобрался», а фильтр «Без решения»
оставляет только то, что требует внимания.

Не путать с seen_at: тот значит «бот показал» и ставится автоматически. Здесь
«я решил» — только руками. Обе колонки нужны, и подменять одну другой нельзя.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.delivery import bot_ui
from src.delivery.bot_ui import _decision_keyboard
from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat
from src.storage import (
    APPLIED,
    REJECTED,
    count_archive,
    get_vacancy,
    save_vacancy,
    set_decision,
    vacancies_page,
)
from src.storage.db import connect

NOW = datetime(2026, 1, 1)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _vacancy(title="QA"):
    return Vacancy(
        raw_text="текст",
        posted_at=NOW,
        is_vacancy=True,
        title=title,
        stack=["Python"],
        work_format=WorkFormat.REMOTE,
        grade=Grade.MIDDLE,
        salary=Salary(min_value=250000, max_value=250000, currency="RUB", raw="250к"),
    )


def _save(conn, hash_value, score="high"):
    save_vacancy(
        conn,
        hash_value=hash_value,
        channel="@ch",
        message_id=1,
        vacancy=_vacancy(),
        score=score,
        reasoning="ок",
        link="https://t.me/ch/1",
        message="карточка",
    )
    conn.commit()
    return conn.execute(
        "SELECT rowid FROM vacancies WHERE content_hash = ?", (hash_value,)
    ).fetchone()["rowid"]


# ---------- хранение ----------


def test_new_vacancy_has_no_decision(conn):
    rowid = _save(conn, "h1")

    assert get_vacancy(conn, rowid)["decision"] is None


def test_set_applied(conn):
    rowid = _save(conn, "h1")

    set_decision(conn, rowid, APPLIED)

    row = get_vacancy(conn, rowid)
    assert row["decision"] == APPLIED
    assert row["decided_at"] is not None


def test_decision_can_be_cleared(conn):
    """Нажал не то — должен уметь снять, а не искать вакансию заново."""
    rowid = _save(conn, "h1")
    set_decision(conn, rowid, REJECTED)

    set_decision(conn, rowid, None)

    row = get_vacancy(conn, rowid)
    assert row["decision"] is None
    assert row["decided_at"] is None


def test_decision_does_not_touch_seen_at(conn):
    """
    seen_at («бот показал») и decision («я решил») — разные вещи. Смешать их
    значит либо потерять вакансию из дайджеста, либо показывать решённое.
    """
    rowid = _save(conn, "h1")
    conn.execute("UPDATE vacancies SET seen_at = ? WHERE rowid = ?", ("2026-01-02", rowid))

    set_decision(conn, rowid, APPLIED)

    assert get_vacancy(conn, rowid)["seen_at"] == "2026-01-02"


def test_decided_at_is_aware_utc(conn):
    """Сервер в UTC: наивное время тут — та же грабля, что с last_fetch."""
    rowid = _save(conn, "h1")

    set_decision(conn, rowid, APPLIED)

    at = datetime.fromisoformat(get_vacancy(conn, rowid)["decided_at"])
    assert at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60


# ---------- фильтр «без решения» ----------


def test_undecided_filter_hides_decided(conn):
    """Ради этого всё и делалось."""
    first = _save(conn, "h1")
    _save(conn, "h2")
    set_decision(conn, first, APPLIED)

    rows = vacancies_page(conn, undecided=True)

    assert [r["content_hash"] for r in rows] == ["h2"]
    assert count_archive(conn, undecided=True) == 1
    assert count_archive(conn) == 2, "в общем списке остаются обе"


def test_rejected_also_counts_as_decided(conn):
    """«Не буду» — тоже решение: вакансия не должна возвращаться в работу."""
    rowid = _save(conn, "h1")
    set_decision(conn, rowid, REJECTED)

    assert vacancies_page(conn, undecided=True) == []


def test_undecided_combines_with_score(conn):
    """Фильтры не должны затирать друг друга."""
    _save(conn, "h1", score="high")
    decided = _save(conn, "h2", score="high")
    _save(conn, "h3", score="medium")
    set_decision(conn, decided, APPLIED)

    rows = vacancies_page(conn, score="high", undecided=True)

    assert [r["content_hash"] for r in rows] == ["h1"]
    assert count_archive(conn, score="high", undecided=True) == 1


def test_page_exposes_rowid(conn):
    """Без rowid кнопке нечего класть в callback_data."""
    _save(conn, "h1")

    assert vacancies_page(conn)[0]["rowid"] is not None


# ---------- кнопки ----------


def test_callback_data_fits_telegram_limit():
    """
    Лимит callback_data — 64 БАЙТА. content_hash сам по себе 64 символа, и
    поэтому ключом стал rowid. Проверяем с запасом на большой id.
    """
    keyboard = _decision_keyboard(999999, None)

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_keyboard_marks_current_decision():
    applied = _decision_keyboard(1, APPLIED).inline_keyboard[0]
    assert "✅" in applied[0].text
    assert "✅" not in applied[1].text

    rejected = _decision_keyboard(1, REJECTED).inline_keyboard[0]
    assert "🚫" in rejected[1].text
    assert "✅" not in rejected[0].text


def test_keyboard_keeps_both_buttons_after_choice():
    """Решение должно переключаться, а не застывать после первого нажатия."""
    assert len(_decision_keyboard(1, APPLIED).inline_keyboard[0]) == 2


# ---------- хендлер ----------


class _Conn:
    def __init__(self, row):
        self.row = row

    def close(self):
        pass


async def test_handler_sets_decision(monkeypatch):
    saved = {}
    monkeypatch.setattr(bot_ui, "connect", lambda: _Conn(None))
    monkeypatch.setattr(bot_ui, "get_vacancy", lambda conn, rowid: {"decision": None})
    monkeypatch.setattr(
        bot_ui, "set_decision", lambda conn, rowid, d: saved.update(rowid=rowid, d=d)
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        data="d:a:7",
        message=SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock()),
    )

    await bot_ui.cb_decision(callback)

    assert saved == {"rowid": 7, "d": APPLIED}


async def test_handler_toggles_same_decision_off(monkeypatch):
    """Повторное нажатие той же кнопки снимает отметку."""
    saved = {}
    monkeypatch.setattr(bot_ui, "connect", lambda: _Conn(None))
    monkeypatch.setattr(bot_ui, "get_vacancy", lambda conn, rowid: {"decision": APPLIED})
    monkeypatch.setattr(
        bot_ui, "set_decision", lambda conn, rowid, d: saved.update(d=d)
    )
    callback = SimpleNamespace(
        answer=AsyncMock(),
        data="d:a:7",
        message=SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock()),
    )

    await bot_ui.cb_decision(callback)

    assert saved == {"d": None}


async def test_handler_survives_missing_vacancy(monkeypatch):
    """reassess мог убрать вакансию, а карточка в чате осталась висеть."""
    monkeypatch.setattr(bot_ui, "connect", lambda: _Conn(None))
    monkeypatch.setattr(bot_ui, "get_vacancy", lambda conn, rowid: None)
    answered = []
    callback = SimpleNamespace(
        answer=AsyncMock(),
        data="d:a:7",
        message=SimpleNamespace(
            answer=AsyncMock(side_effect=lambda t, **k: answered.append(t)),
            edit_reply_markup=AsyncMock(),
        ),
    )

    await bot_ui.cb_decision(callback)

    assert "больше нет" in answered[0]
