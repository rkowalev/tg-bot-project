"""
Тесты конвейера целиком. Telethon, Claude и Telegram подменены — проверяем
логику этапов и их порядок, а не сеть.

Главное: повторный прогон не должен ни слать те же вакансии, ни звать ИИ.
"""

from datetime import datetime

import pytest

from src.filters.criteria import Criteria
from src.filters.relevance import RelevanceResult, Score
from src.models.vacancy import WorkFormat
from src.pipeline import pipeline as pipeline_module
from src.sources.telegram import RawPost

NOW = datetime(2026, 1, 1)

REMOTE_PYTHON = """#вакансия
Вакансия: AQA Engineer (Python)
Компания: Ромашка
Формат работы: удаленно
Вилка: 250-330к на руки
Стек: Python, pytest"""

OFFICE_POST = """#вакансия
Вакансия: QA Engineer
Формат работы: Очно Казань
Вилка: 250-300к
Стек: Python"""


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Подменяет источник, ИИ и бота. Возвращает счётчики вызовов."""
    calls = {"enrich": 0, "assess": 0, "sent": []}

    def set_posts(posts: list[RawPost]):
        async def fake_iter(limit, channels=None):
            for post in posts:
                yield post

        monkeypatch.setattr(pipeline_module, "iter_posts", fake_iter)

    async def fake_enrich(vacancy):
        calls["enrich"] += 1
        enriched = vacancy.model_copy(deep=True)
        enriched.is_vacancy = True
        return enriched

    async def fake_filter(vacancy, criteria):
        from src.filters.filter import FilterResult
        from src.filters.rules import passes_hard_rules

        ok, reasons = passes_hard_rules(vacancy, criteria)
        if not ok:
            return FilterResult(passed=False, reasons=reasons)
        calls["assess"] += 1
        return FilterResult(passed=True, score=Score.HIGH, reasoning="подходит")

    async def fake_send(bot, vacancy, result, link):
        calls["sent"].append(vacancy.title)
        return True

    monkeypatch.setattr(pipeline_module, "enrich_vacancy", fake_enrich)
    monkeypatch.setattr(pipeline_module, "filter_vacancy", fake_filter)
    monkeypatch.setattr(pipeline_module, "send_vacancy", fake_send)

    calls["set_posts"] = set_posts
    calls["db"] = tmp_path / "test.db"
    return calls


class FakeBot:
    """Досылка зовёт bot.send_message напрямую — фейк должен это уметь."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)


def _post(message_id: int, text: str) -> RawPost:
    return RawPost(channel="@ch", message_id=message_id, text=text, posted_at=NOW)


CRITERIA = Criteria(work_formats=[WorkFormat.REMOTE], stack_include=["Python"])


# ---------- дедуп: повторный прогон ----------


async def test_second_run_delivers_nothing_and_calls_no_ai(wire):
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])

    first = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert first.delivered == 1
    assert wire["enrich"] == 1

    second = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert second.delivered == 0, "повторный прогон не должен слать то же самое"
    assert second.already_seen == 1
    assert wire["enrich"] == 1, "ИИ на виденном посте звать нельзя — это деньги"
    assert wire["assess"] == 1
    assert wire["sent"] == ["AQA Engineer (Python)"], "отправка ровно одна"


async def test_new_post_delivered_after_seen_one(wire):
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])

    other = REMOTE_PYTHON.replace("Ромашка", "Лютик")
    wire["set_posts"]([_post(1, REMOTE_PYTHON), _post(2, other)])
    stats = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])

    assert stats.already_seen == 1
    assert stats.delivered == 1, "новый пост должен доехать"
    assert len(wire["sent"]) == 2


# ---------- дедуп по содержимому: репост с другим id ----------


async def test_repost_with_different_id_is_not_delivered_twice(wire):
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])

    # тот же текст, но другой message_id и лишнее оформление — это репост
    wire["set_posts"]([_post(99, "**" + REMOTE_PYTHON + "**  ")])
    stats = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])

    assert stats.already_seen == 0, "id новый — уровень 1 его не поймает"
    assert stats.duplicates == 1, "поймать должен уровень 2 — хэш текста"
    assert stats.delivered == 0
    assert len(wire["sent"]) == 1


# ---------- предфильтр: ИИ не зовётся на заведомо мимо ----------


async def test_prefilter_saves_ai_call(wire):
    wire["set_posts"]([_post(1, OFFICE_POST)])
    stats = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])

    assert stats.cut_by_prefilter == 1
    assert wire["enrich"] == 0, "офисную вакансию отсекли до ИИ — вызова быть не должно"
    assert stats.delivered == 0


async def test_prefiltered_post_marked_seen(wire):
    wire["set_posts"]([_post(1, OFFICE_POST)])
    await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    stats = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert stats.already_seen == 1, "отсеянный пост тоже виден — второй раз не парсим"


# ---------- доставка ----------


async def test_dry_run_saves_but_does_not_send(wire):
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    stats = await pipeline_module.run_once(CRITERIA, bot=None, limit=10, db_path=wire["db"])
    assert stats.delivered == 0
    assert wire["sent"] == []


async def test_failed_send_is_not_marked_delivered(wire, monkeypatch):
    async def failing_send(bot, vacancy, result, link):
        return False

    monkeypatch.setattr(pipeline_module, "send_vacancy", failing_send)
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    stats = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert stats.delivered == 0

    from src.storage import connect

    conn = connect(wire["db"])
    row = conn.execute("SELECT delivered_at FROM vacancies").fetchone()
    conn.close()
    assert row["delivered_at"] is None, "не отправилось — не помечаем доставленным"


# ---------- регрессия: DRY_RUN не должен съедать вакансии ----------


async def test_dry_run_does_not_swallow_vacancy(wire):
    """
    Баг с живого прогона: DRY_RUN сохранял вакансию в БД до проверки бота,
    и на первом РЕАЛЬНОМ запуске она считалась дублем и не приходила никогда.
    """
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    dry = await pipeline_module.run_once(CRITERIA, bot=None, limit=10, db_path=wire["db"])
    assert dry.delivered == 0

    # тот же пост, теперь с живым ботом — вакансия ОБЯЗАНА доехать
    bot = FakeBot()
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    real = await pipeline_module.run_once(CRITERIA, bot=bot, limit=10, db_path=wire["db"])
    assert real.delivered == 1, "после DRY_RUN вакансия потерялась — это баг"
    assert real.redelivered == 1, "должна уйти из очереди, а не парситься заново"
    assert len(bot.sent) == 1, "боту обязано прийти сообщение"
    assert "AQA Engineer" in bot.sent[0]


async def test_failed_send_is_retried_next_run(wire, monkeypatch):
    """Упавшая отправка не должна терять вакансию — досылаем на следующем проходе."""
    async def failing(bot, vacancy, result, link):
        return False

    monkeypatch.setattr(pipeline_module, "send_vacancy", failing)
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    first = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert first.delivered == 0

    async def working(bot, vacancy, result, link):
        wire["sent"].append(vacancy.title)
        return True

    monkeypatch.setattr(pipeline_module, "send_vacancy", working)
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    second = await pipeline_module.run_once(CRITERIA, bot=FakeBot(), limit=10, db_path=wire["db"])
    assert second.delivered == 1, "недоставленное должно досылаться"


# ---------- регрессия: БД, созданная СТАРОЙ схемой ----------


async def test_legacy_rows_without_message_are_recovered(wire):
    """
    Реальный случай с прогона владельца: 9 вакансий записаны старым кодом,
    когда колонки message ещё не было. Они лежат с delivered_at=NULL и
    message=NULL, а посты помечены виденными — то есть заново не прочитаются.
    Такие записи ОБЯЗАНЫ досылаться, иначе вакансии потеряны навсегда.
    """
    import sqlite3

    # воспроизводим БД в состоянии "до миграции"
    raw = sqlite3.connect(wire["db"])
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
        "INSERT INTO vacancies VALUES ('h1','@ch',1,'Senior AQA Python','Ромашка',"
        "'senior','remote',250000,330000,'@hr','high','стек совпал',"
        "'https://t.me/ch/1','2026-01-01T00:00:00',NULL)"
    )
    raw.execute("INSERT INTO seen_posts VALUES ('@ch', 1, '2026-01-01T00:00:00')")
    raw.commit()
    raw.close()

    bot = FakeBot()
    wire["set_posts"]([_post(1, REMOTE_PYTHON)])
    stats = await pipeline_module.run_once(CRITERIA, bot=bot, limit=10, db_path=wire["db"])

    assert stats.already_seen == 1, "пост виден — заново парсить не должны"
    assert stats.redelivered == 1, "старая запись обязана досылаться"
    assert len(bot.sent) == 1
    assert "Senior AQA Python" in bot.sent[0]
    assert "250" in bot.sent[0], "зарплата из БД должна попасть в сводку"
