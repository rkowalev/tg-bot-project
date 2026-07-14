"""
Словарь технологий — ЕДИНЫЙ источник истины.

Зачем отдельным файлом: раньше список жил внутри parsing/parser.py, а критерии
фильтра ссылались на технологии свободными строками. Ничто их не связывало,
поэтому "pytest" в критериях молча не матчился — парсер такого слова не знал.
Теперь и парсер (что извлекать), и Criteria (что можно спросить) смотрят сюда,
а опечатка в критериях падает валидацией, а не теряется молча.

Регистр в записях канонический — он попадает в Vacancy.stack как есть.
Сам поиск в тексте регистронезависимый.
"""

STACK_VOCABULARY: list[str] = [
    # языки
    "Python", "Java", "Kotlin", "Swift", "JavaScript", "TypeScript", "Bash",
    # автотесты
    "Playwright", "Selenium", "Appium", "Cypress", "pytest",
    # API и сеть
    "API", "REST", "Postman", "Swagger", "Charles",
    # нагрузка
    "JMeter", "k6",
    # БД
    "SQL", "PostgreSQL", "MySQL", "MongoDB",
    # инфраструктура
    "Docker", "Kubernetes", "CI/CD", "Jenkins", "Git", "Linux",
    # процессы и отчётность
    "Jira", "Allure", "TestRail", "Grafana",
]

# Нормализованный вид -> канонический, чтобы критерии можно было писать
# как угодно ("python", "PyTest"), а сравнивать однозначно.
_CANONICAL = {tech.lower(): tech for tech in STACK_VOCABULARY}


def canonical(tech: str) -> str | None:
    """Приводит название технологии к каноническому виду. None — если не знаем такой."""
    return _CANONICAL.get(tech.strip().lower())
