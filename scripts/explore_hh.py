"""
Итерация 0 — разведка hh API.

Разовый скрипт: один запрос к /vacancies, чтобы своими глазами увидеть СЫРОЙ ответ
и понять, какие поля приходят, что бывает null, как устроена зарплата.
В конвейер (src/) НЕ входит. Запуск: .venv/bin/python scripts/explore_hh.py
"""

import json

import requests

# hh отклоняет запросы без User-Agent — это его требование, не общая норма HTTP.
# Формат свободный: имя приложения + контакт.
HEADERS = {"User-Agent": "vacancy-bot-learning (ruslankowalev.com@gmail.com)"}

URL = "https://api.hh.ru/vacancies"

# Query-параметры hh. per_page маленький — на разведке хватит 5 вакансий.
PARAMS = {
    "text": "QA Automation",
    "per_page": 5,
    "page": 0,
}


def main() -> None:
    response = requests.get(URL, headers=HEADERS, params=PARAMS, timeout=10)
    response.raise_for_status()  # бросит исключение на HTTP-ошибке (4xx/5xx)

    data = response.json()  # это обычный dict, НЕ Pydantic-модель

    # Верхний уровень ответа — объект со счётчиками и списком items.
    print("=== Верхний уровень ответа ===")
    print(f"found (всего нашлось): {data['found']}")
    print(f"pages (страниц):       {data['pages']}")
    print(f"per_page / page:       {data['per_page']} / {data['page']}")
    print(f"в items пришло:        {len(data['items'])}")

    # Полностью одна вакансия — главное, ради чего скрипт.
    # ensure_ascii=False, чтобы кириллица была читаемой, а не \uXXXX.
    print("\n=== Одна вакансия целиком (items[0]) ===")
    print(json.dumps(data["items"][0], ensure_ascii=False, indent=2))

    # Отдельно смотрим на salary по всем 5 — чтобы поймать null своими глазами.
    print("\n=== Поле salary по всем вакансиям ===")
    for item in data["items"]:
        print(f"- {item['name'][:50]:50}  salary = {item['salary']}")


if __name__ == "__main__":
    main()
