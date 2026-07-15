"""
Замок на обход каналов: одновременно идёт максимум один.

Зачем. Обход запускают ДВА разных процесса: крон (`daily_fetch.py`) и бот по
кнопке «Проверить сейчас» (`run_bot.py`). Оба лезут в один и тот же
`.session` Telethon. Одновременное использование одного session-файла даёт
AUTH_KEY_DUPLICATED — Telegram отзывает авторизацию техаккаунта, и чинится это
только повторным логином с кодом. Достаточно нажать кнопку в 7:00, когда
проснулся крон, или просто дважды подряд.

Почему flock, а не флаг в БД: ОС снимает flock сама, когда процесс умирает.
Флаг в таблице после падения прогона остался бы навсегда, и обход был бы
заблокирован до ручной правки.

Замок НЕ ЖДЁТ (LOCK_NB): если обход уже идёт, второму сказать «занято» честнее,
чем молча держать пользователя минуту в неведении.
"""

import fcntl
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent.parent / ".fetch.lock"


class FetchBusy(RuntimeError):
    """Обход уже идёт в другом процессе."""


@contextmanager
def fetch_lock():
    with LOCK_PATH.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FetchBusy("обход каналов уже идёт") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
