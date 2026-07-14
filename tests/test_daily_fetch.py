"""
Тесты фонового сбора. Проверяем главное требование: пауза отсекает ВСЁ
внешнее до единого вызова, и дайджест уходит один и только когда есть что.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

from src.filters.criteria import Criteria
from src.models.vacancy import Vacancy, WorkFormat
from src.storage import db

NOW = datetime(2026, 1, 1)


def _load_script():
    """daily_fetch лежит в scripts/ и пакетом не является — грузим по пути."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "daily_fetch.py"
    spec = importlib.util.spec_from_file_location("daily_fetch", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["daily_fetch"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch, tmp_path):
    module = _load_script()
    calls = {"pipeline": 0, "bot_made": 0, "sent": []}
    path = tmp_path / "t.db"

    # БД у скрипта берётся через connect() без аргумента — подменяем путь
    monkeypatch.setattr(db, "DB_PATH", path)

    class FakeBot:
        class _Session:
            async def close(self):
                pass

        session = _Session()

        async def send_message(self, chat_id, text, **kwargs):
            calls["sent"].append({"text": text, "silent": kwargs.get("disable_notification")})

    def fake_make_bot():
        calls["bot_made"] += 1
        return FakeBot()

    async def fake_run_once(criteria, bot, limit, db_path=None):
        calls["pipeline"] += 1
        calls["bot_passed"] = bot
        conn = db.connect(path)
        try:
            db.save_vacancy(
                conn,
                hash_value=f"h{calls['pipeline']}",
                channel="@ch",
                message_id=calls["pipeline"],
                vacancy=Vacancy(raw_text="t", posted_at=NOW, title="Новая"),
                score="high",
                reasoning="ок",
                link="https://t.me/ch/1",
                message="сводка",
            )
            conn.commit()
        finally:
            conn.close()

        class Stats:
            def report(self):
                return "(отчёт)"

        return Stats()

    monkeypatch.setattr(module, "make_bot", fake_make_bot)
    monkeypatch.setattr(module, "run_once", fake_run_once)

    calls["module"] = module
    calls["db"] = path
    return calls


def _setup_criteria(path):
    conn = db.connect(path)
    try:
        db.save_criteria(conn, Criteria(work_formats=[WorkFormat.REMOTE]))
    finally:
        conn.close()


# ---------- пауза: ничего не тратим ----------


async def test_paused_fetch_does_nothing_at_all(script):
    _setup_criteria(script["db"])
    conn = db.connect(script["db"])
    db.set_fetch_enabled(conn, False)
    conn.close()

    await script["module"].main()

    assert script["pipeline"] == 0, "на паузе pipeline звать нельзя — это Telethon и ИИ"
    assert script["bot_made"] == 0, "на паузе даже бота создавать не надо"
    assert script["sent"] == []


async def test_resumed_fetch_runs(script):
    _setup_criteria(script["db"])
    conn = db.connect(script["db"])
    db.set_fetch_enabled(conn, False)
    db.set_fetch_enabled(conn, True)
    conn.close()

    await script["module"].main()
    assert script["pipeline"] == 1


# ---------- без критериев не лезем в сеть ----------


async def test_no_criteria_means_no_fetch(script):
    await script["module"].main()
    assert script["pipeline"] == 0, "без резюме собирать нечего и не по чему"
    assert script["sent"] == []


# ---------- дайджест ----------


async def test_digest_is_single_and_silent(script):
    _setup_criteria(script["db"])
    await script["module"].main()

    assert len(script["sent"]) == 1, "ровно одно сообщение, а не по вакансии на штуку"
    assert script["sent"][0]["silent"] is True, "дайджест обязан быть тихим"
    assert "1" in script["sent"][0]["text"]


async def test_pipeline_gets_no_bot(script):
    """Конвейер не должен слать вакансии по одной — это делает дайджест."""
    _setup_criteria(script["db"])
    await script["module"].main()
    assert script["bot_passed"] is None


async def test_no_new_means_silence(script):
    _setup_criteria(script["db"])
    await script["module"].main()
    assert len(script["sent"]) == 1

    # второй прогон: pipeline ничего нового не кладёт
    async def barren_run(criteria, bot, limit, db_path=None):
        class Stats:
            def report(self):
                return "(отчёт)"

        return Stats()

    script["module"].run_once = barren_run

    # помечаем показанным то, что уже есть
    conn = db.connect(script["db"])
    db.mark_seen_vacancy(conn, "h1")
    conn.commit()
    conn.close()

    await script["module"].main()
    assert len(script["sent"]) == 1, "новых нет — писать нельзя, это спам"


async def test_last_fetch_is_recorded(script):
    _setup_criteria(script["db"])
    await script["module"].main()
    conn = db.connect(script["db"])
    try:
        assert db.get_last_fetch_at(conn) is not None
    finally:
        conn.close()
