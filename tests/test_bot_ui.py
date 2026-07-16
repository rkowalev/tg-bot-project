"""
Роутинг бота: кнопки меню не должны тонуть в состояниях FSM.

Хендлеры состояний в aiogram зарегистрированы раньше кнопочных, поэтому без
явного фильтра состояние забирает текст кнопки себе. Так «Показать новые» в
состоянии waiting_resume уезжало в парсер резюме.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from src.delivery import bot_ui
from src.delivery.bot_ui import MENU_BUTTONS, NOT_MENU_BUTTON, main_keyboard
from src.filters.criteria import Criteria
from src.models.vacancy import WorkFormat
from src.pipeline import RunStats

BOT_ID = 42


def _message(text):
    return SimpleNamespace(text=text)


@pytest.mark.parametrize("button", sorted(MENU_BUTTONS))
def test_state_handlers_skip_menu_buttons(button):
    assert NOT_MENU_BUTTON.resolve(_message(button)) is False


def test_state_handlers_take_real_text():
    assert NOT_MENU_BUTTON.resolve(_message("Senior QA, Python, Playwright")) is True


def test_state_handlers_take_documents():
    """У файла text=None — резюме файлом обязано доходить до got_resume."""
    assert NOT_MENU_BUTTON.resolve(_message(None)) is True


@pytest.mark.parametrize("fetch_on", [True, False])
def test_every_keyboard_button_is_known(fetch_on):
    """
    Забыть добавить новую кнопку в MENU_BUTTONS — значит вернуть баг.
    Надпись паузы зависит от состояния, поэтому проверяем оба варианта.
    """
    on_keyboard = {
        button.text for row in main_keyboard(fetch_on).keyboard for button in row
    }
    assert on_keyboard <= MENU_BUTTONS


# ---------- роутинг целиком: от нажатия до хендлера ----------


class _FakeConn:
    def close(self):
        pass

    def commit(self):
        pass


@pytest.fixture
def routing(monkeypatch):
    """
    Диспетчер с памятью вместо БД и заглушкой вместо сети.
    Собирает, какие хендлеры реально сработали на апдейт.
    """
    calls = []

    monkeypatch.setattr(Bot, "__call__", AsyncMock(return_value=None))
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())

    async def fake_parse_resume(text):
        calls.append("parse_resume")
        return Criteria()

    monkeypatch.setattr(bot_ui, "parse_resume", fake_parse_resume)
    monkeypatch.setattr(
        bot_ui, "unseen_vacancies", lambda conn: calls.append("unseen_vacancies") or []
    )

    bot = Bot(token=f"{BOT_ID}:TEST")
    dispatcher = Dispatcher(storage=MemoryStorage())
    # router — синглтон модуля и цепляется только к одному диспетчеру,
    # поэтому на каждый тест отцепляем его от предыдущего
    monkeypatch.setattr(bot_ui.router, "_parent_router", None)
    dispatcher.include_router(bot_ui.router)

    async def feed(text, state=None):
        context = FSMContext(
            storage=dispatcher.storage,
            key=StorageKey(bot_id=BOT_ID, chat_id=1, user_id=1),
        )
        if state is not None:
            await context.set_state(state)
        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="R"),
            text=text,
        ).as_(bot)
        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
        return calls, await context.get_state()

    return feed


async def test_show_new_works_while_waiting_for_resume(routing):
    """Баг: нажал «Обновить резюме», ничего не прислал, жмёшь «Показать новые» —
    и текст кнопки уезжает в парсер резюме вместо показа вакансий."""
    calls, state = await routing(bot_ui.BTN_NEW, bot_ui.Onboarding.waiting_resume)

    assert calls == ["unseen_vacancies"]
    assert state is None, "кнопка обязана погасить состояние, иначе съест следующий текст"


async def test_stale_digest_button_still_shows_vacancies(monkeypatch):
    """
    Крон шлёт дайджест, даже когда бот не запущен. Нажатие ждёт в очереди
    Telegram, и на протухшую query ответить уже нельзя — но вакансии обязаны
    доехать, а не сгинуть вместе с ошибкой.
    """
    calls = []
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        bot_ui, "unseen_vacancies", lambda conn: calls.append("unseen_vacancies") or []
    )

    async def stale_answer(*args, **kwargs):
        raise TelegramBadRequest(method="answerCallbackQuery", message="query is too old")

    callback = SimpleNamespace(
        answer=stale_answer,
        message=SimpleNamespace(answer=AsyncMock()),
    )
    state = AsyncMock()

    await bot_ui.cb_show_new(callback, state)

    assert calls == ["unseen_vacancies"], "протухшая кнопка не должна отменять показ"


# ---------- обход по кнопке ----------


@pytest.fixture
def fetching(monkeypatch):
    """Конвейер подменён: сети в тестах нет. Возвращает, звали ли обход."""
    calls = []
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "get_criteria", lambda conn: Criteria())
    monkeypatch.setattr(bot_ui, "is_fetch_enabled", lambda conn: True)
    monkeypatch.setattr(bot_ui, "count_unseen", lambda conn: len(calls) and 3 or 0)

    async def fake_run_once(criteria, bot=None, limit=None):
        calls.append(limit)
        return RunStats()

    monkeypatch.setattr(bot_ui, "run_once", fake_run_once)
    return calls


def _msg():
    sent = []
    return SimpleNamespace(
        answer=AsyncMock(side_effect=lambda text, **kw: sent.append(text)), sent=sent
    )


async def test_fetch_button_runs_pipeline(fetching):
    message = _msg()

    await bot_ui.btn_fetch_now(message, AsyncMock())

    assert fetching == [bot_ui.FETCH_LIMIT], "обход обязан сходить с тем же лимитом"
    assert "Иду по каналам" in message.sent[0], "ожидание должно быть честно объявлено"


async def test_fetch_button_reports_busy_and_skips_run(fetching):
    """
    Крон проснулся в тот же момент или нажали дважды. Второй обход НЕ должен
    стартовать: один .session на два разом = AUTH_KEY_DUPLICATED.
    """
    message = _msg()

    with bot_ui.fetch_lock():
        await bot_ui.btn_fetch_now(message, AsyncMock())

    assert fetching == [], "второй обход не должен был стартовать"
    assert "Уже иду" in message.sent[0]
    assert not any("Иду по каналам" in t for t in message.sent), (
        "обещать обход, которого не будет, нельзя"
    )


async def test_fetch_button_respects_pause(fetching, monkeypatch):
    """Пауза значит «не трогай каналы» — кнопка не исключение."""
    monkeypatch.setattr(bot_ui, "is_fetch_enabled", lambda conn: False)
    message = _msg()

    await bot_ui.btn_fetch_now(message, AsyncMock())

    assert fetching == []
    assert "паузе" in message.sent[0]


async def test_show_buttons_never_touch_network(routing, monkeypatch):
    """
    Кнопки показа обязаны остаться мгновенными. Обход в них = минута ожидания
    там, где данные уже лежат в БД.
    """
    ran = []

    async def boom(*args, **kwargs):
        ran.append(1)

    monkeypatch.setattr(bot_ui, "run_once", boom)

    await routing(bot_ui.BTN_NEW)

    assert ran == [], "кнопка показа сходила в сеть"


# ---------- окно срезов по дате ----------


async def test_period_window_is_utc_not_local(monkeypatch):
    """
    posted_at приходит из Telethon с поясом (+00:00), а vacancies_since
    сравнивает СТРОКАМИ. Наивное datetime.now() сдвигало окно на величину
    пояса машины: на ноутбуке (МСК) «За сегодня» молча теряло посты за
    последние 3 часа. На UTC-сервере совпало случайно — но зависеть от пояса
    машины выборка не должна.
    """
    asked = {}
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        bot_ui,
        "vacancies_since",
        lambda conn, since: asked.update(since=since) or [],
    )

    await bot_ui.btn_today(_msg(), AsyncMock())

    since = asked["since"]
    assert since.tzinfo is not None, "окно без пояса поедет вслед за поясом машины"
    assert since.utcoffset() == timedelta(0), "posted_at в UTC — окно тоже в UTC"


# ---------- архив и правка критериев ----------


def _row(title, score="medium"):
    return {"message": f"карточка: {title}", "content_hash": title, "score": score}


async def test_criteria_edit_reuses_card_without_resume(monkeypatch):
    """
    Ради этого всё и делалось: поправить формат раньше можно было только
    перезаливкой резюме, хотя формата в резюме нет вообще.
    """
    criteria = Criteria(work_formats=[WorkFormat.REMOTE, WorkFormat.HYBRID])
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "get_criteria", lambda conn: criteria)
    parse = AsyncMock()
    monkeypatch.setattr(bot_ui, "parse_resume", parse)

    answers = []
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock(side_effect=lambda *a, **k: answers.append(a))),
    )
    state = AsyncMock()

    await bot_ui.cb_criteria_edit(callback, state)

    parse.assert_not_awaited()  # резюме перечитывать не должны
    state.set_state.assert_awaited_with(bot_ui.Onboarding.confirming)
    assert "удалёнка" in answers[0][0], "в карточке должны быть текущие критерии"


async def test_criteria_menu_offers_both_ways(monkeypatch):
    """Кнопка больше не гонит за резюме силой — предлагает выбор."""
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "get_criteria", lambda conn: Criteria())
    sent = {}
    message = SimpleNamespace(
        answer=AsyncMock(side_effect=lambda text, **kw: sent.update(kw))
    )

    await bot_ui.btn_criteria(message, AsyncMock())

    labels = [b.text for row in sent["reply_markup"].inline_keyboard for b in row]
    assert any("Поправить" in x for x in labels)
    assert any("резюме" in x for x in labels)


async def test_archive_shows_seen_and_old(monkeypatch):
    """Архив не смотрит ни на seen_at, ни на возраст — иначе он бесполезен."""
    asked = {}
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "count_vacancies", lambda conn, score=None: 3)
    monkeypatch.setattr(
        bot_ui,
        "vacancies_page",
        lambda conn, score=None, limit=10, offset=0: asked.update(
            score=score, offset=offset
        )
        or [_row("старая, уже показанная")],
    )
    callback = SimpleNamespace(
        answer=AsyncMock(), data="all:any:0", message=SimpleNamespace(answer=AsyncMock())
    )

    await bot_ui.cb_all_page(callback)

    assert asked == {"score": None, "offset": 0}


async def test_archive_filters_by_high(monkeypatch):
    asked = {}
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "count_vacancies", lambda conn, score=None: 1)
    monkeypatch.setattr(
        bot_ui,
        "vacancies_page",
        lambda conn, score=None, limit=10, offset=0: asked.update(score=score)
        or [_row("хай", "high")],
    )
    callback = SimpleNamespace(
        answer=AsyncMock(), data="all:high:0", message=SimpleNamespace(answer=AsyncMock())
    )

    await bot_ui.cb_all_page(callback)

    assert asked == {"score": "high"}


async def test_archive_paging_offsets_forward(monkeypatch):
    """«Показать ещё» обязана вести на следующую пачку, а не на ту же."""
    monkeypatch.setattr(bot_ui, "connect", lambda: _FakeConn())
    monkeypatch.setattr(bot_ui, "count_vacancies", lambda conn, score=None: 25)
    monkeypatch.setattr(
        bot_ui,
        "vacancies_page",
        lambda conn, score=None, limit=10, offset=0: [_row(f"v{i}") for i in range(10)],
    )
    sent = []
    callback = SimpleNamespace(
        answer=AsyncMock(),
        data="all:any:10",
        message=SimpleNamespace(answer=AsyncMock(side_effect=lambda text, **kw: sent.append((text, kw)))),
    )

    await bot_ui.cb_all_page(callback)

    tail_text, tail_kw = sent[-1]
    assert "20 из 25" in tail_text
    next_button = tail_kw["reply_markup"].inline_keyboard[0][0]
    assert next_button.callback_data == "all:any:20"


async def test_resume_text_still_reaches_parser(routing):
    """Обратная сторона: обычный текст в том же состоянии должен дойти до парсера."""
    calls, _ = await routing(
        "Senior QA Engineer, Python, Playwright, pytest", bot_ui.Onboarding.waiting_resume
    )

    assert calls == ["parse_resume"]
