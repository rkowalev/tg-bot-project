"""
Тесты ручной правки критериев в боте.

Контекст: резюме владельца было без зарплаты, а кнопка "Заново" перепарсила бы
тот же текст и вернула ту же пустоту. Значит поля надо править руками — и
разбор этого ввода должен быть предсказуемым.
"""

import pytest

from src.profile.inputs import InputError, parse_languages, parse_salary


# ---------- зарплата ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("230", 230_000),  # голое число — это тысячи, как у людей принято
        ("230000", 230_000),
        ("230к", 230_000),
        ("230k", 230_000),
        ("230 000", 230_000),
        ("230.000", 230_000),
        ("от 230", 230_000),
        ("от 230000 руб", 230_000),
        ("230 тыс", 230_000),
        ("хочу 250к на руки", 250_000),
    ],
)
def test_salary_formats(text, expected):
    assert parse_salary(text) == expected


def test_salary_rejects_text():
    with pytest.raises(InputError, match="Не понял число"):
        parse_salary("побольше")


def test_salary_rejects_empty():
    with pytest.raises(InputError):
        parse_salary("")


def test_salary_rejects_absurdly_small():
    # 5 -> 5000: явно опечатка, а не зарплата в месяц
    with pytest.raises(InputError, match="слишком мало"):
        parse_salary("5")


def test_salary_rejects_absurdly_large():
    with pytest.raises(InputError, match="опечатка"):
        parse_salary("230000000")


# ---------- языки ----------


def test_single_language():
    assert parse_languages("Python") == ["Python"]


def test_language_case_normalized():
    assert parse_languages("python") == ["Python"]


@pytest.mark.parametrize(
    "text", ["Python, Go", "Python и Go", "Python/Go", "Python Go", "python; go"]
)
def test_language_separators(text):
    assert parse_languages(text) == ["Python", "Go"]


def test_language_aliases():
    assert parse_languages("golang, c#") == ["Go", "C#"]


def test_duplicates_collapsed():
    assert parse_languages("Python, python") == ["Python"]


def test_unknown_language_is_reported_not_swallowed():
    """
    В резюме незнакомое молча отбрасываем — там ИИ мог ошибиться. Здесь человек
    ответил на прямой вопрос: проглотить его ответ = соврать, что приняли.
    """
    with pytest.raises(InputError, match="Не знаю такой язык: Rust"):
        parse_languages("Python, Rust")


def test_tool_is_not_accepted_as_language():
    with pytest.raises(InputError, match="Docker"):
        parse_languages("Docker")


def test_empty_input_lists_known_languages():
    with pytest.raises(InputError, match="Python"):
        parse_languages("   ")
