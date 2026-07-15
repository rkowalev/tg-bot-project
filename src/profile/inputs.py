"""
Разбор того, что человек правит руками в боте.

Зачем: резюме отвечает не на все вопросы. Зарплатных ожиданий в нём часто нет
вовсе — а это второй по важности жёсткий критерий после языка. Кнопка "Заново"
тут не помогала: перепарсить то же резюме = получить тот же пустой min_salary.

Это НЕ парсинг вакансий: там текст чужой и грязный, здесь человек отвечает на
конкретный вопрос. Поэтому правила проще и ошибку можно показать в лицо.
"""

import re

from config.stack import LANGUAGES, canonical, is_language

_NUMBER = re.compile(r"(\d[\d\s.,]*)\s*(к|k|тыс)?", re.IGNORECASE)


class InputError(ValueError):
    """Текст показывается человеку — он должен объяснять, как надо."""


def parse_salary(text: str) -> int:
    """
    "230" / "230к" / "230 000" / "от 230000" / "230 тыс" -> 230000.

    Голое число до 1000 считаем тысячами: человек, пишущий "230", имеет в виду
    230к, а не 230 рублей. Ровно та же логика, что у парсера вакансий.
    """
    match = _NUMBER.search(text or "")
    if not match:
        raise InputError("Не понял число. Напиши, например: 230 или 230000")

    digits = re.sub(r"[\s.,]", "", match.group(1))
    if not digits:
        raise InputError("Не понял число. Напиши, например: 230 или 230000")

    value = int(digits)
    if match.group(2) or value < 1000:
        value *= 1000

    if value < 10_000:
        raise InputError(f"{value} — слишком мало для зарплаты в месяц. Проверь число.")
    if value > 2_000_000:
        raise InputError(f"{value // 1000}к в месяц — это опечатка? Проверь число.")
    return value


def parse_languages(text: str) -> list[str]:
    """
    "Python" / "python, go" / "Python и Go" -> канонические имена.

    Незнакомое НЕ отбрасываем молча, как при парсинге резюме: тут человек
    ответил на прямой вопрос, и если он ошибся — надо сказать, а не проглотить.
    """
    parts = [p.strip() for p in re.split(r"[,/;]|\s+и\s+|\s+", text or "") if p.strip()]
    if not parts:
        raise InputError(f"Напиши язык. Известные: {', '.join(LANGUAGES)}")

    result: list[str] = []
    unknown: list[str] = []
    for part in parts:
        name = canonical(part)
        if name is None or not is_language(name):
            unknown.append(part)
        elif name not in result:
            result.append(name)

    if unknown:
        raise InputError(
            f"Не знаю такой язык: {', '.join(unknown)}.\n"
            f"Известные: {', '.join(LANGUAGES)}"
        )
    return result
