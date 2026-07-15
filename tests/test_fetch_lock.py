"""
Замок на обход каналов.

Цена ошибки: крон и бот делят один .session Telethon. Два обхода разом дают
AUTH_KEY_DUPLICATED — Telegram отзывает авторизацию техаккаунта, и лечится это
только повторным логином с кодом из SMS.
"""

import multiprocessing
import time

import pytest

from src.pipeline import FetchBusy, fetch_lock


def test_second_lock_in_same_process_is_busy():
    with fetch_lock():
        with pytest.raises(FetchBusy):
            with fetch_lock():
                pass


def test_lock_released_after_block():
    with fetch_lock():
        pass
    # не должно бросить: замок отпущен
    with fetch_lock():
        pass


def test_lock_released_after_exception():
    """Упал обход — замок обязан отпуститься, иначе кнопка мертва навсегда."""
    with pytest.raises(ValueError):
        with fetch_lock():
            raise ValueError("обход упал")

    with fetch_lock():
        pass


def _hold_lock(started, release):
    with fetch_lock():
        started.set()
        release.wait(timeout=10)


def test_lock_works_across_processes():
    """
    Главное. Крон и бот — РАЗНЫЕ процессы, и замок обязан их развести. Замок,
    который держит только внутри процесса, тут бесполезен.
    """
    started = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold_lock, args=(started, release))
    holder.start()
    try:
        assert started.wait(timeout=10), "дочерний процесс не взял замок"
        with pytest.raises(FetchBusy):
            with fetch_lock():
                pass
    finally:
        release.set()
        holder.join(timeout=10)

    # чужой процесс закончил — замок снова свободен
    with fetch_lock():
        pass


def test_lock_freed_when_holder_dies():
    """
    Процесс убит на середине обхода (OOM, kill) — ОС снимает flock сама.
    Ради этого он и выбран: флаг в БД остался бы висеть навсегда.
    """
    started = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold_lock, args=(started, release))
    holder.start()
    assert started.wait(timeout=10)

    holder.kill()
    holder.join(timeout=10)
    time.sleep(0.1)  # ОС нужен момент, чтобы прибрать за процессом

    with fetch_lock():
        pass
