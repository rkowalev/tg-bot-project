"""
UX бота: онбординг резюме (FSM) + клавиатура.

Режим — pull с тихим дайджестом. Бот НЕ пушит вакансии по одной: раз в сутки
крон присылает одно тихое «доступно N новых», всё остальное — по кнопке и
мгновенно из БД. Ни одна кнопка показа не трогает Telethon и не зовёт ИИ.

Резюме парсится ТОЛЬКО на онбординге и обновлении — результат живёт в БД.
"""

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from src.delivery.telegram_bot import format_message_from_row
from src.profile import ResumeParseError, describe, parse_resume
from src.storage import (
    connect,
    count_unseen,
    get_criteria,
    is_fetch_enabled,
    mark_seen_vacancy,
    save_criteria,
    set_fetch_enabled,
    unseen_vacancies,
    vacancies_since,
)

router = Router()

BTN_NEW = "🆕 Показать новые"
BTN_TODAY = "За сегодня"
BTN_3DAYS = "За 3 дня"
BTN_WEEK = "За неделю"
BTN_RESUME = "📄 Обновить резюме"
BTN_PAUSE = "⏸ Остановить поиск"
BTN_RESUME_SEARCH = "▶️ Возобновить поиск"

# Telegram не даст отправить 50 сообщений подряд — упрёмся в лимит и получим
# 429. Показываем пачкой, остальное останется непоказанным до следующего раза.
BATCH_LIMIT = 10


class Onboarding(StatesGroup):
    waiting_resume = State()
    confirming = State()


def main_keyboard(fetch_on: bool) -> ReplyKeyboardMarkup:
    """Надпись паузы зависит от состояния — иначе непонятно, что нажимаешь."""
    pause = KeyboardButton(text=BTN_PAUSE if fetch_on else BTN_RESUME_SEARCH)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [
                KeyboardButton(text=BTN_TODAY),
                KeyboardButton(text=BTN_3DAYS),
                KeyboardButton(text=BTN_WEEK),
            ],
            [KeyboardButton(text=BTN_RESUME), pause],
        ],
        resize_keyboard=True,
    )


_CONFIRM = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Верно, сохранить", callback_data="crit_ok"),
            InlineKeyboardButton(text="🔄 Заново", callback_data="crit_retry"),
        ]
    ]
)

digest_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Показать", callback_data="show_new")]]
)


def _ask_resume() -> str:
    return (
        "Пришли своё резюме — текстом в сообщении или файлом .txt\n\n"
        "Я вытащу из него стек, грейд, желаемый формат и зарплатные ожидания, "
        "покажу что понял, и после твоего подтверждения начну искать вакансии."
    )


# ---------- онбординг ----------


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    conn = connect()
    try:
        criteria = get_criteria(conn)
        if criteria is None:
            await state.set_state(Onboarding.waiting_resume)
            await message.answer(_ask_resume())
            return
        await message.answer(
            f"Ищу по твоим критериям:\n\n{describe(criteria)}",
            reply_markup=main_keyboard(is_fetch_enabled(conn)),
        )
    finally:
        conn.close()


@router.message(F.text == BTN_RESUME)
async def btn_update_resume(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.waiting_resume)
    await message.answer(_ask_resume())


async def _extract_text(message: Message) -> str | None:
    """Текст сообщения или содержимое .txt. pdf/docx осознанно не поддержаны."""
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith(".txt"):
            await message.answer(
                "Пока понимаю только .txt или текст сообщением.\n"
                "Скопируй резюме текстом или сохрани как .txt"
            )
            return None
        buffer = await message.bot.download(message.document)
        return buffer.read().decode("utf-8", errors="replace")
    return message.text


@router.message(Onboarding.waiting_resume)
async def got_resume(message: Message, state: FSMContext) -> None:
    text = await _extract_text(message)
    if text is None:
        return

    await message.answer("Читаю резюме…")
    try:
        criteria = await parse_resume(text)
    except ResumeParseError as error:
        await message.answer(f"{error}")
        return

    # НЕ сохраняем молча — сначала показываем, что поняли
    await state.update_data(criteria=criteria.model_dump_json())
    await state.set_state(Onboarding.confirming)
    await message.answer(
        f"Понял так:\n\n{describe(criteria)}\n\nВсё верно?", reply_markup=_CONFIRM
    )


@router.callback_query(F.data == "crit_ok", Onboarding.confirming)
async def confirm_criteria(callback: CallbackQuery, state: FSMContext) -> None:
    from src.filters.criteria import Criteria

    data = await state.get_data()
    criteria = Criteria.model_validate_json(data["criteria"])

    conn = connect()
    try:
        save_criteria(conn, criteria)
        fetch_on = is_fetch_enabled(conn)
    finally:
        conn.close()

    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Сохранил. Раз в сутки буду проверять каналы и тихо присылать, "
        "если появится что-то по твоим критериям.",
        reply_markup=main_keyboard(fetch_on),
    )
    await callback.answer()


@router.callback_query(F.data == "crit_retry", Onboarding.confirming)
async def retry_criteria(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Onboarding.waiting_resume)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(_ask_resume())
    await callback.answer()


# ---------- показ вакансий: всегда из БД ----------


async def _send_rows(message: Message, rows: list, mark_as_seen: bool) -> None:
    if not rows:
        await message.answer("Ничего нет.")
        return

    shown = rows[:BATCH_LIMIT]
    conn = connect() if mark_as_seen else None
    try:
        for row in shown:
            text = row["message"] or format_message_from_row(row)
            await message.answer(text, disable_web_page_preview=True)
            if conn is not None:
                mark_seen_vacancy(conn, row["content_hash"])
        if conn is not None:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    if len(rows) > BATCH_LIMIT:
        await message.answer(
            f"Показал {BATCH_LIMIT} из {len(rows)}. Нажми ещё раз, чтобы увидеть остальные."
        )


@router.message(F.text == BTN_NEW)
async def btn_new(message: Message) -> None:
    conn = connect()
    try:
        rows = unseen_vacancies(conn)
    finally:
        conn.close()
    if not rows:
        await message.answer("Новых нет.")
        return
    await _send_rows(message, rows, mark_as_seen=True)


@router.callback_query(F.data == "show_new")
async def cb_show_new(callback: CallbackQuery) -> None:
    await callback.answer()
    await btn_new(callback.message)


async def _show_period(message: Message, days: int, label: str) -> None:
    since = datetime.now() - timedelta(days=days)
    conn = connect()
    try:
        rows = vacancies_since(conn, since)
    finally:
        conn.close()
    if not rows:
        await message.answer(f"{label}: ничего.")
        return
    await message.answer(f"{label}: {len(rows)}")
    # срез — просто просмотр, seen не трогаем: иначе "за неделю" съест новые
    await _send_rows(message, rows, mark_as_seen=False)


@router.message(F.text == BTN_TODAY)
async def btn_today(message: Message) -> None:
    await _show_period(message, 1, "За сегодня")


@router.message(F.text == BTN_3DAYS)
async def btn_3days(message: Message) -> None:
    await _show_period(message, 3, "За 3 дня")


@router.message(F.text == BTN_WEEK)
async def btn_week(message: Message) -> None:
    await _show_period(message, 7, "За неделю")


# ---------- пауза сбора ----------


@router.message(F.text.in_({BTN_PAUSE, BTN_RESUME_SEARCH}))
async def btn_toggle_fetch(message: Message) -> None:
    conn = connect()
    try:
        new_state = not is_fetch_enabled(conn)
        set_fetch_enabled(conn, new_state)
        unseen = count_unseen(conn)
    finally:
        conn.close()

    if new_state:
        text = "Поиск возобновлён. Проверю каналы вечером."
    else:
        text = "Поиск остановлен — каналы больше не опрашиваю, API не трачу."
        if unseen:
            text += f"\nВ базе осталось непоказанных: {unseen}."
    await message.answer(text, reply_markup=main_keyboard(new_state))


@router.message()
async def fallback(message: Message) -> None:
    conn = connect()
    try:
        has_criteria = get_criteria(conn) is not None
        fetch_on = is_fetch_enabled(conn)
    finally:
        conn.close()
    if not has_criteria:
        await message.answer("Сначала пришли резюме — жми /start")
        return
    await message.answer("Не понял. Жми кнопки.", reply_markup=main_keyboard(fetch_on))
