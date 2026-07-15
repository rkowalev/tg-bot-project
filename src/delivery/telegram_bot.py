"""
Доставка сводок — обычный Telegram-бот через aiogram.

НЕ ПУТАТЬ с sources/telegram.py: там Telethon читает каналы под техаккаунтом
(api_id/api_hash + файл сессии). Здесь — бот от @BotFather под BOT_TOKEN,
который пишет владельцу в личку. Разные механизмы, разные креды.

Здесь — рендер сводок и создание клиента. Хендлеры кнопок и онбординг живут в
bot_ui.py, меню команд ставится из кода (setup_commands), а не через @BotFather.

Каналы этот бот не опрашивает: их читает Telethon по крону раз в сутки.
"""

import html
import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from dotenv import load_dotenv

from src.filters.filter import FilterResult
from src.models.vacancy import Vacancy

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

_SCORE_BADGE = {
    "high": "🟢 HIGH",
    "medium": "🟡 MEDIUM",
    "low": "⚪️ LOW",
    # пост-заглушка: решать по ссылке, а не по описанию
    "no_data": "🔗 НЕТ ОПИСАНИЯ",
}

# Сводку читает человек по-русски — enum'ы наружу не показываем.
_FORMAT_RU = {
    "remote": "удалёнка",
    "hybrid": "гибрид",
    "office": "офис",
    "unknown": "не указан",
}
_GRADE_RU = {
    "junior": "junior",
    "middle": "middle",
    "senior": "senior",
    "lead": "lead",
    "unknown": "не указан",
}


def _salary_line(vacancy: Vacancy) -> str:
    if not vacancy.salary or vacancy.salary.min_value is None:
        return "не указана"
    gross = {True: "гросс", False: "на руки", None: ""}[vacancy.salary.gross]
    low = vacancy.salary.min_value // 1000
    high = (vacancy.salary.max_value or vacancy.salary.min_value) // 1000
    span = f"{low}к" if low == high else f"{low}–{high}к"
    period = "/час" if vacancy.salary.period == "hour" else ""
    line = f"{span}{period} {gross}".strip()
    if vacancy.salary_alternatives:
        line += f" (ещё: {', '.join(vacancy.salary_alternatives)})"
    return line


def format_message(vacancy: Vacancy, result: FilterResult, link: str) -> str:
    """HTML: текст вакансий приходит из чужих постов, экранируем всё подряд."""
    esc = html.escape

    def row(label: str, value: str | None) -> str:
        return f"<b>{label}:</b> {esc(value)}\n" if value else ""

    badge = _SCORE_BADGE.get(result.score.value if result.score else "", "⚪️ —")
    title = esc(vacancy.title or "Должность не распознана")

    text = f"{badge}  <b>{title}</b>\n\n"
    text += row("Компания", vacancy.company)
    text += row("Зарплата", _salary_line(vacancy))
    text += row(
        "Формат",
        _FORMAT_RU.get(vacancy.work_format.value) if vacancy.work_format else None,
    )
    text += row("Грейд", _GRADE_RU.get(vacancy.grade.value) if vacancy.grade else None)
    text += row("Стек", ", ".join(vacancy.stack) if vacancy.stack else None)
    if result.reasoning:
        text += f"\n<i>{esc(result.reasoning)}</i>\n"
    if vacancy.contact:
        text += f"\n<b>Отклик:</b> {esc(vacancy.contact)}\n"
    text += f'\n<a href="{esc(link)}">Пост в канале</a>'
    return text


def format_message_from_row(row) -> str:
    """
    Собирает сводку из строки БД, когда готового текста нет.

    Нужно для записей, сделанных до появления колонки message: они лежат
    недоставленными, а посты уже помечены виденными — заново через конвейер их
    не прогнать. Стека и альтернативных вилок в таблице нет, поэтому сводка
    выйдет чуть беднее — но вакансия дойдёт, а это важнее.
    """
    esc = html.escape

    def row_line(label: str, value) -> str:
        return f"<b>{label}:</b> {esc(str(value))}\n" if value else ""

    badge = _SCORE_BADGE.get(row["score"] or "", "⚪️ —")
    text = f"{badge}  <b>{esc(row['title'] or 'Должность не распознана')}</b>\n\n"
    text += row_line("Компания", row["company"])

    if row["salary_min"]:
        low = row["salary_min"] // 1000
        high = (row["salary_max"] or row["salary_min"]) // 1000
        text += row_line("Зарплата", f"{low}к" if low == high else f"{low}–{high}к")

    text += row_line("Формат", _FORMAT_RU.get(row["work_format"] or ""))
    text += row_line("Грейд", _GRADE_RU.get(row["grade"] or ""))
    if row["reasoning"]:
        text += f"\n<i>{esc(row['reasoning'])}</i>\n"
    if row["contact"]:
        text += f"\n<b>Отклик:</b> {esc(row['contact'])}\n"
    if row["link"]:
        text += f'\n<a href="{esc(row["link"])}">Пост в канале</a>'
    return text


async def send_vacancy(bot: Bot, vacancy: Vacancy, result: FilterResult, link: str) -> bool:
    """True — доставлено. Ошибка отправки не роняет прогон."""
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=format_message(vacancy, result, link),
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as error:
        print(f"  !! не отправилось: {error}")
        return False


async def setup_commands(bot: Bot) -> None:
    """
    Меню команд (кнопка ☰ рядом с полем ввода).

    Через @BotFather это делать не надо — команды ставятся из кода. Нужно
    потому, что кнопка START видна только в НОВОМ чате: как только бот однажды
    что-то прислал, она пропадает, и /start становится негде взять.
    """
    await bot.set_my_commands(
        [BotCommand(command="start", description="Запустить / показать критерии")]
    )


def make_bot() -> Bot:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден в .env — возьми у @BotFather")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID не найден в .env — свой id можно узнать у @userinfobot")
    return Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
