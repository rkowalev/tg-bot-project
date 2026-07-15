"""
Роутинг бота: кнопки меню не должны тонуть в состояниях FSM.

Хендлеры состояний в aiogram зарегистрированы раньше кнопочных, поэтому без
явного фильтра состояние забирает текст кнопки себе. Так «Показать новые» в
состоянии waiting_resume уезжало в парсер резюме.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from src.delivery import bot_ui
from src.delivery.bot_ui import MENU_BUTTONS, NOT_MENU_BUTTON, main_keyboard
from src.filters.criteria import Criteria

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


async def test_resume_text_still_reaches_parser(routing):
    """Обратная сторона: обычный текст в том же состоянии должен дойти до парсера."""
    calls, _ = await routing(
        "Senior QA Engineer, Python, Playwright, pytest", bot_ui.Onboarding.waiting_resume
    )

    assert calls == ["parse_resume"]
