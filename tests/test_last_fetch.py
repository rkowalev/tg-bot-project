"""
Отметка «обход был»: время + сколько нашли.

Смысл: без неё «Новых нет» неотличимо от «бот умер». Владелец уже наступил —
ждал утренний дайджест, не дождался и пошёл искать поломку, которой не было
(прогон отработал, просто за ночь не нашлось ни одной подходящей вакансии).

Пояс тут не мелочь: сервер живёт в UTC, владелец в Москве. Показать 04:02
вместо 07:02 — значит подтвердить его подозрение, что прогон не сработал.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.bot_ui import _last_fetch_line
from src.storage import get_last_fetch_at, get_last_fetch_found, touch_last_fetch
from src.storage.db import connect, get_setting, set_setting

MOSCOW = ZoneInfo("Europe/Moscow")


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_touch_writes_timezone_into_db(conn):
    """
    Проверяем СЫРОЕ значение в БД, а не то, что вернуло чтение: get_last_fetch_at
    само дописывает UTC наивным строкам (ради legacy-записей), и через него
    наивная запись выглядела бы корректной. Записав наивное время на машине в
    МСК, мы прочитали бы его как UTC — то есть показали бы время на 3 часа
    вперёд, из будущего.
    """
    touch_last_fetch(conn, 3)

    raw = get_setting(conn, "last_fetch_at")
    assert raw.endswith("+00:00"), f"время записано без пояса: {raw}"
    assert get_last_fetch_found(conn) == 3


def test_touch_reads_back_as_utc(conn):
    touch_last_fetch(conn, 1)

    at = get_last_fetch_at(conn)
    assert at.tzinfo is not None
    assert at.utcoffset() == timedelta(0)
    # записанное время не должно убегать от реального UTC
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60


def test_naive_legacy_value_is_read_as_utc(conn):
    """
    Записи до появления пояса лежат наивными. Их пишет сервер, а он в UTC —
    трактовать их как локальное время значило бы соврать на 3 часа.
    """
    set_setting(conn, "last_fetch_at", "2026-07-16T04:02:55")

    at = get_last_fetch_at(conn)

    assert at.utcoffset() == timedelta(0)
    assert at.hour == 4


def test_line_shows_moscow_time_not_utc(conn):
    """
    Главное. Прогон в 04:02 UTC — это 07:02 по Москве, и владелец должен
    увидеть именно 07:02: он сверяет с расписанием «07:00 по будням».
    """
    set_setting(conn, "last_fetch_at", "2026-07-16T04:02:55+00:00")
    set_setting(conn, "last_fetch_found", "0")

    line = _last_fetch_line(conn)

    assert "07:02" in line, f"показали не московское время: {line}"
    assert "04:02" not in line


def test_line_says_zero_found_explicitly(conn):
    """«Не нашёл» — это ответ. Молчание — нет."""
    touch_last_fetch(conn, 0)

    assert "новых не нашёл" in _last_fetch_line(conn)


def test_line_reports_found_count(conn):
    touch_last_fetch(conn, 5)

    assert "нашёл новых: 5" in _last_fetch_line(conn)


def test_line_without_any_fetch(conn):
    assert _last_fetch_line(conn) == "Обхода ещё не было."


@pytest.mark.parametrize(
    "shift, expected",
    [(timedelta(0), "сегодня"), (timedelta(days=1), "вчера"), (timedelta(days=3), "в")],
)
def test_line_wording_by_age(conn, shift, expected):
    at = datetime.now(MOSCOW).replace(hour=12, minute=0) - shift
    set_setting(conn, "last_fetch_at", at.astimezone(timezone.utc).isoformat())
    set_setting(conn, "last_fetch_found", "1")

    assert expected in _last_fetch_line(conn)
