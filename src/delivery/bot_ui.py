"""
UX бота: онбординг резюме (FSM) + клавиатура.

Режим — pull с тихим дайджестом. Бот НЕ пушит вакансии по одной: крон присылает
одно тихое «доступно N новых», всё остальное — по кнопке.

Кнопки делятся на два сорта, и это principial:
  - ПОКАЗА (Показать новые, За сегодня/3 дня/неделю, Все вакансии) — только
    читают БД. Мгновенно, ни Telethon, ни ИИ.
  - ОБХОДА (Проверить сейчас) — единственная, что идёт в сеть. Под замком:
    крон и бот делят один .session, одновременный обход = AUTH_KEY_DUPLICATED.

Свести их в одну кнопку нельзя: после утреннего дайджеста обход вернул бы то
же самое, а ждать пришлось бы.

Резюме парсится ТОЛЬКО на онбординге и обновлении — результат живёт в БД.
"""

import os
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
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
from src.models.vacancy import WorkFormat
from src.pipeline import FetchBusy, fetch_lock, run_once
from src.profile import (
    DocumentError,
    InputError,
    ResumeParseError,
    describe,
    extract_text,
    parse_languages,
    parse_resume,
    parse_salary,
)
from src.storage import (
    connect,
    count_unseen,
    count_vacancies,
    get_criteria,
    is_fetch_enabled,
    mark_seen_vacancy,
    save_criteria,
    set_fetch_enabled,
    unseen_vacancies,
    vacancies_page,
    vacancies_since,
)

router = Router()

BTN_NEW = "🆕 Показать новые"
# Отдельно от BTN_NEW осознанно. Это РАЗНЫЕ операции: BTN_NEW отдаёт мгновенно
# то, что уже нашли, BTN_FETCH идёт по каналам заново. Свести их в одну кнопку
# значит заставить ждать обхода сразу после утреннего дайджеста — крон только
# что сходил, и обход вернёт ровно то же самое.
BTN_FETCH = "🔄 Проверить сейчас"
BTN_TODAY = "За сегодня"
BTN_3DAYS = "За 3 дня"
BTN_WEEK = "За неделю"
BTN_ALL = "📋 Все вакансии"
BTN_CRITERIA = "⚙️ Критерии"
BTN_PAUSE = "⏸ Остановить поиск"
BTN_RESUME_SEARCH = "▶️ Возобновить поиск"

# Кнопки меню приходят обычным текстом. Хендлеры состояний зарегистрированы
# раньше кнопочных и иначе съедали бы нажатие: "Показать новые" в состоянии
# waiting_resume уезжало в парсер резюме. Состояния текст кнопок пропускают,
# кнопки — гасят состояние.
MENU_BUTTONS = frozenset(
    {
        BTN_NEW,
        BTN_FETCH,
        BTN_TODAY,
        BTN_3DAYS,
        BTN_WEEK,
        BTN_ALL,
        BTN_CRITERIA,
        BTN_PAUSE,
        BTN_RESUME_SEARCH,
    }
)
NOT_MENU_BUTTON = ~F.text.in_(MENU_BUTTONS)

# Telegram не даст отправить 50 сообщений подряд — упрёмся в лимит и получим
# 429. Показываем пачкой, остальное останется непоказанным до следующего раза.
BATCH_LIMIT = 10

# Сколько постов брать с канала при ручном обходе. Тот же LIMIT, что у крона:
# иначе кнопка и расписание видели бы разную глубину канала.
FETCH_LIMIT = int(os.environ.get("LIMIT", "50"))


class Onboarding(StatesGroup):
    waiting_resume = State()
    confirming = State()
    # Правка полей прямо в боте. Нужна потому, что резюме отвечает не на все
    # вопросы: зарплатных ожиданий в нём часто нет, а "Заново" перепарсит тот
    # же текст и вернёт ту же пустоту.
    editing_salary = State()
    editing_languages = State()


def main_keyboard(fetch_on: bool) -> ReplyKeyboardMarkup:
    """Надпись паузы зависит от состояния — иначе непонятно, что нажимаешь."""
    pause = KeyboardButton(text=BTN_PAUSE if fetch_on else BTN_RESUME_SEARCH)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_FETCH)],
            [
                KeyboardButton(text=BTN_TODAY),
                KeyboardButton(text=BTN_3DAYS),
                KeyboardButton(text=BTN_WEEK),
            ],
            [KeyboardButton(text=BTN_ALL)],
            [KeyboardButton(text=BTN_CRITERIA), pause],
        ],
        resize_keyboard=True,
    )


_FORMAT_LABELS = {
    WorkFormat.REMOTE: "удалёнка",
    WorkFormat.HYBRID: "гибрид",
    WorkFormat.OFFICE: "офис",
}


def _confirm_keyboard(criteria) -> InlineKeyboardMarkup:
    """
    Форматы — переключатели прямо в карточке: их всего три, отдельный экран
    ради них избыточен. Зарплата и язык — свободный ввод, там нужен вопрос.
    """
    formats = [
        InlineKeyboardButton(
            text=f"{'✅' if fmt in criteria.work_formats else '⬜'} {label}",
            callback_data=f"fmt_{fmt.value}",
        )
        for fmt, label in _FORMAT_LABELS.items()
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Зарплата", callback_data="edit_salary"),
                InlineKeyboardButton(text="🔤 Язык", callback_data="edit_languages"),
            ],
            formats,
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data="crit_ok"),
                InlineKeyboardButton(text="🔄 Другое резюме", callback_data="crit_retry"),
            ],
        ]
    )

digest_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Показать", callback_data="show_new")]]
)


def _ask_resume() -> str:
    return (
        "Пришли своё резюме — текстом в сообщении или файлом (PDF, TXT)\n\n"
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
        await state.clear()
        await message.answer(
            f"Ищу по твоим критериям:\n\n{describe(criteria)}",
            reply_markup=main_keyboard(is_fetch_enabled(conn)),
        )
    finally:
        conn.close()


def _criteria_menu() -> InlineKeyboardMarkup:
    """
    Резюме — не единственный способ поменять критерии. Поправить формат или
    порог зарплаты через перезаливку резюме нельзя в принципе: этих полей в
    резюме нет, парсер вернёт ту же пустоту.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Поправить текущие", callback_data="crit_edit")],
            [
                InlineKeyboardButton(
                    text="📄 Загрузить новое резюме", callback_data="crit_new"
                )
            ],
        ]
    )


@router.message(F.text == BTN_CRITERIA)
async def btn_criteria(message: Message, state: FSMContext) -> None:
    await state.clear()
    conn = connect()
    try:
        criteria = get_criteria(conn)
    finally:
        conn.close()

    if criteria is None:
        # править нечего — сразу за резюме
        await state.set_state(Onboarding.waiting_resume)
        await message.answer(_ask_resume())
        return

    await message.answer(
        f"Сейчас ищу так:\n\n{describe(criteria)}", reply_markup=_criteria_menu()
    )


@router.callback_query(F.data == "crit_edit")
async def cb_criteria_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Критерии из БД -> та же карточка, что и после разбора резюме."""
    with suppress(TelegramBadRequest):
        await callback.answer()

    conn = connect()
    try:
        criteria = get_criteria(conn)
    finally:
        conn.close()

    if criteria is None:
        await state.set_state(Onboarding.waiting_resume)
        await callback.message.answer(_ask_resume())
        return

    await _store(state, criteria)
    await state.set_state(Onboarding.confirming)
    await _show_card(callback.message, criteria)


@router.callback_query(F.data == "crit_new")
async def cb_criteria_new_resume(callback: CallbackQuery, state: FSMContext) -> None:
    with suppress(TelegramBadRequest):
        await callback.answer()
    await state.set_state(Onboarding.waiting_resume)
    await callback.message.answer(_ask_resume())


# Резюме с hh.ru весит пару сотен КБ. Всё, что сильно больше, — не резюме,
# и качать это в память незачем.
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024


async def _extract_text(message: Message) -> str | None:
    """Текст сообщения или содержимое файла. None -> ответ уже отправлен."""
    if not message.document:
        return message.text

    document = message.document
    if document.file_size and document.file_size > MAX_DOCUMENT_BYTES:
        await message.answer("Файл больше 5 МБ — это вряд ли резюме. Пришли текстом.")
        return None

    buffer = await message.bot.download(document)
    try:
        return extract_text(document.file_name or "", buffer.read())
    except DocumentError as error:
        await message.answer(str(error))
        return None


@router.message(Onboarding.waiting_resume, NOT_MENU_BUTTON)
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
    await _show_card(message, criteria)


def _card_text(criteria) -> str:
    hint = ""
    if criteria.min_salary is None:
        # это главный пробел: в резюме зарплаты обычно нет, а критерий важный
        hint = "\n\n⚠️ Зарплата не указана — без неё пропущу вакансии с любой вилкой."
    # Заголовок нейтральный: карточка открывается и после разбора резюме, и по
    # кнопке правки критериев.
    return f"Ищу так:\n\n{describe(criteria)}{hint}\n\nМожно поправить кнопками."


async def _show_card(message: Message, criteria) -> None:
    await message.answer(_card_text(criteria), reply_markup=_confirm_keyboard(criteria))


async def _refresh_card(callback: CallbackQuery, criteria) -> None:
    """Перерисовываем ту же карточку, а не плодим новые сообщения."""
    await callback.message.edit_text(
        _card_text(criteria), reply_markup=_confirm_keyboard(criteria)
    )


async def _criteria_from_state(state: FSMContext):
    from src.filters.criteria import Criteria

    data = await state.get_data()
    return Criteria.model_validate_json(data["criteria"])


async def _store(state: FSMContext, criteria) -> None:
    await state.update_data(criteria=criteria.model_dump_json())


# ---------- правка полей ----------


@router.callback_query(F.data == "edit_salary", Onboarding.confirming)
async def ask_salary(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Onboarding.editing_salary)
    await callback.message.answer(
        "Напиши желаемую зарплату в месяц.\nНапример: <code>230</code> или "
        "<code>230000</code>"
    )
    await callback.answer()


@router.message(Onboarding.editing_salary, NOT_MENU_BUTTON)
async def got_salary(message: Message, state: FSMContext) -> None:
    try:
        salary = parse_salary(message.text or "")
    except InputError as error:
        await message.answer(str(error))
        return

    criteria = await _criteria_from_state(state)
    criteria.min_salary = salary
    await _store(state, criteria)
    await state.set_state(Onboarding.confirming)
    await _show_card(message, criteria)


@router.callback_query(F.data == "edit_languages", Onboarding.confirming)
async def ask_languages(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Onboarding.editing_languages)
    await callback.message.answer(
        "Напиши языки, на которых готов работать, через запятую.\n"
        "Например: <code>Python</code> или <code>Python, Go</code>\n\n"
        "Вакансии на других языках буду отсекать."
    )
    await callback.answer()


@router.message(Onboarding.editing_languages, NOT_MENU_BUTTON)
async def got_languages(message: Message, state: FSMContext) -> None:
    try:
        languages = parse_languages(message.text or "")
    except InputError as error:
        await message.answer(str(error))
        return

    criteria = await _criteria_from_state(state)
    criteria.languages = languages
    await _store(state, criteria)
    await state.set_state(Onboarding.confirming)
    await _show_card(message, criteria)


@router.callback_query(F.data.startswith("fmt_"), Onboarding.confirming)
async def toggle_format(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = WorkFormat(callback.data.removeprefix("fmt_"))
    criteria = await _criteria_from_state(state)

    if fmt in criteria.work_formats:
        criteria.work_formats = [f for f in criteria.work_formats if f is not fmt]
    else:
        criteria.work_formats = [*criteria.work_formats, fmt]

    await _store(state, criteria)
    await _refresh_card(callback, criteria)
    await callback.answer()


@router.callback_query(F.data == "crit_ok", Onboarding.confirming)
async def confirm_criteria(callback: CallbackQuery, state: FSMContext) -> None:
    criteria = await _criteria_from_state(state)

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
        if mark_as_seen:
            # показанное помечено -> следующее нажатие отдаст следующую пачку
            await message.answer(
                f"Показал {BATCH_LIMIT} из {len(rows)}. "
                "Нажми ещё раз, чтобы увидеть остальные."
            )
        else:
            # срез по дате seen не трогает: повторное нажатие вернёт ТЕ ЖЕ 10,
            # обещать «остальные» тут нельзя — за ними в «Все вакансии».
            await message.answer(
                f"Показал {BATCH_LIMIT} из {len(rows)}, свежие сверху. "
                f"Остальные — в «{BTN_ALL}»."
            )


@router.message(F.text == BTN_NEW)
async def btn_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    conn = connect()
    try:
        rows = unseen_vacancies(conn)
    finally:
        conn.close()
    if not rows:
        await message.answer("Новых нет.")
        return
    await _send_rows(message, rows, mark_as_seen=True)


# ---------- обход по требованию: единственная кнопка, которая ходит в сеть ----------


@router.message(F.text == BTN_FETCH)
async def btn_fetch_now(message: Message, state: FSMContext) -> None:
    """
    Ручной обход. Нужен между утренними прогонами: посмотрел вчерашние, написал
    HR, а после обеда хочешь свежие — крон до завтра не проснётся.
    """
    await state.clear()
    conn = connect()
    try:
        criteria = get_criteria(conn)
        fetch_on = is_fetch_enabled(conn)
        before = count_unseen(conn)
    finally:
        conn.close()

    if criteria is None:
        await message.answer("Сначала пришли резюме — жми /start")
        return
    if not fetch_on:
        await message.answer(
            "Поиск на паузе. Возобнови его, если хочешь сходить по каналам.",
            reply_markup=main_keyboard(False),
        )
        return

    try:
        # FetchBusy летит из __enter__, поэтому "Иду по каналам" не напечатается,
        # если замок занят: сначала лезем за ним, потом обещаем.
        with fetch_lock():
            await message.answer(
                "Иду по каналам. Обычно это меньше минуты — пришлю, как закончу."
            )
            # bot=None: конвейер только собирает в БД. Рассылку по одной вакансии
            # не включаем — итог отдаём одним сообщением ниже.
            started = time.perf_counter()
            stats = await run_once(criteria, bot=None, limit=FETCH_LIMIT)
            # В journald (StandardOutput=journal у юнита). Без этого обход по
            # кнопке не оставлял НИКАКОГО следа: "новых нет" за две секунды
            # выглядит одинаково и когда всё отработало, и когда не запускалось
            # вовсе. Проверять такое по mtime файла замка — не дело.
            print(
                f"обход по кнопке за {time.perf_counter() - started:.1f} с\n"
                f"{stats.report()}",
                flush=True,
            )
    except FetchBusy:
        # Крон проснулся в тот же момент или кнопку нажали дважды. Один
        # .session на два одновременных обхода = AUTH_KEY_DUPLICATED.
        await message.answer("Уже иду по каналам — подожди, пришлю как закончу.")
        return

    conn = connect()
    try:
        found = count_unseen(conn) - before
    finally:
        conn.close()

    if found <= 0:
        await message.answer("Новых нет — всё, что было, ты уже видел.")
        return
    await message.answer(f"Нашёл новых: {found}", reply_markup=digest_keyboard)


@router.callback_query(F.data == "show_new")
async def cb_show_new(callback: CallbackQuery, state: FSMContext) -> None:
    # Дайджест уходит по крону, а бот живёт в открытом терминале — нажатие
    # запросто пролежит в очереди Telegram, пока бота нет. На протухшую query
    # ответить уже нельзя, но это лишь крутилка на кнопке: вакансии всё равно
    # обязаны доехать, поэтому глушим ошибку, а не весь хендлер.
    with suppress(TelegramBadRequest):
        await callback.answer()
    await btn_new(callback.message, state)


# ---------- архив: всё, что есть, включая показанное и старое ----------


def _all_filter_keyboard(high: int, total: int) -> InlineKeyboardMarkup:
    """low в БД не попадает, поэтому выбор ровно один: только high или всё."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🟢 Только high ({high})", callback_data="all:high:0"
                ),
                InlineKeyboardButton(text=f"📋 Все ({total})", callback_data="all:any:0"),
            ]
        ]
    )


@router.message(F.text == BTN_ALL)
async def btn_all(message: Message, state: FSMContext) -> None:
    await state.clear()
    conn = connect()
    try:
        total = count_vacancies(conn)
        high = count_vacancies(conn, score="high")
    finally:
        conn.close()

    if not total:
        await message.answer("В базе пока пусто.")
        return

    await message.answer("Что показать?", reply_markup=_all_filter_keyboard(high, total))


@router.callback_query(F.data.startswith("all:"))
async def cb_all_page(callback: CallbackQuery) -> None:
    """
    Архив листается через offset в callback_data: состояние тут не нужно и
    только мешало бы — кнопка «Показать ещё» должна работать и через сутки.
    """
    with suppress(TelegramBadRequest):
        await callback.answer()

    _, score_key, raw_offset = callback.data.split(":")
    score = None if score_key == "any" else score_key
    offset = int(raw_offset)

    conn = connect()
    try:
        rows = vacancies_page(conn, score=score, limit=BATCH_LIMIT, offset=offset)
        total = count_vacancies(conn, score=score)
    finally:
        conn.close()

    if not rows:
        await callback.message.answer("Ничего нет.")
        return

    # seen не трогаем: архив — это просмотр, а не выдача новых
    for row in rows:
        text = row["message"] or format_message_from_row(row)
        await callback.message.answer(text, disable_web_page_preview=True)

    shown = offset + len(rows)
    if shown < total:
        await callback.message.answer(
            f"Показал {shown} из {total}.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Показать ещё",
                            callback_data=f"all:{score_key}:{shown}",
                        )
                    ]
                ]
            ),
        )
    else:
        await callback.message.answer(f"Это всё: {total}.")


async def _show_period(
    message: Message, state: FSMContext, days: int, label: str
) -> None:
    await state.clear()
    # UTC, а не now(): posted_at приходит из Telethon с поясом (+00:00), а
    # vacancies_since сравнивает их СТРОКАМИ. Наивное локальное время сдвигало
    # окно на величину пояса — на ноутбуке (МСК) "За сегодня" молча теряло
    # посты за последние 3 часа. На UTC-сервере совпало случайно, но ставить
    # это в зависимость от пояса машины нельзя.
    since = datetime.now(timezone.utc) - timedelta(days=days)
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
async def btn_today(message: Message, state: FSMContext) -> None:
    await _show_period(message, state, 1, "За сегодня")


@router.message(F.text == BTN_3DAYS)
async def btn_3days(message: Message, state: FSMContext) -> None:
    await _show_period(message, state, 3, "За 3 дня")


@router.message(F.text == BTN_WEEK)
async def btn_week(message: Message, state: FSMContext) -> None:
    await _show_period(message, state, 7, "За неделю")


# ---------- пауза сбора ----------


@router.message(F.text.in_({BTN_PAUSE, BTN_RESUME_SEARCH}))
async def btn_toggle_fetch(message: Message, state: FSMContext) -> None:
    await state.clear()
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
