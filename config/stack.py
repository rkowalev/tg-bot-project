"""
Словарь технологий — ЕДИНЫЙ источник истины.

Разделён на ЯЗЫКИ и ИНСТРУМЕНТЫ, и это не косметика, а разная семантика в фильтре:

  ЯЗЫК — жёсткий критерий. Учить новый язык ради вакансии человек не станет,
  поэтому "стек Java" при Python-профиле отсекается правилами, бесплатно.

  ИНСТРУМЕНТ — мягкий. Нет Docker в резюме, а в вакансии он нужен? Выучить не
  проблема, выкидывать такую вакансию нельзя. Инструменты правилами НЕ отсекают,
  их учитывает только ИИ-оценка.

До этого разделения всё лежало в одном списке, правило было "любое пересечение
проходит", и Java-вакансии просачивались через Selenium. Владелец поймал это
на живых данных: из 9 доставленных 4 оказались с чужим языком.

Регистр канонический — он попадает в Vacancy.stack как есть.
Поиск в тексте регистронезависимый.
"""

# Языки программирования. Отдельный список — по нему работает жёсткое правило.
LANGUAGES: list[str] = [
    "Python", "Java", "Kotlin", "Swift", "JavaScript", "TypeScript",
    "C#", "Go", "Ruby", "PHP", "Scala", "C++",
]

# Инструменты, фреймворки, БД. Учить с нуля — вопрос недели, поэтому мягкие.
TOOLS: list[str] = [
    # автотесты
    "Playwright", "Selenium", "Appium", "Cypress", "pytest",
    # API и сеть
    "API", "REST", "Postman", "Swagger", "Charles",
    # нагрузка
    "JMeter", "k6",
    # БД
    "SQL", "PostgreSQL", "MySQL", "MongoDB",
    # инфраструктура
    "Docker", "Kubernetes", "CI/CD", "Jenkins", "Git", "Linux", "Bash",
    # процессы и отчётность
    "Jira", "Allure", "TestRail", "Grafana",
]

STACK_VOCABULARY: list[str] = LANGUAGES + TOOLS

# Синонимы -> канон. Golang и .NET в постах встречаются чаще, чем Go и C#.
_ALIASES = {
    "golang": "Go",
    "c sharp": "C#",
    "csharp": "C#",
    ".net": "C#",
    "dotnet": "C#",
    "js": "JavaScript",
    "ts": "TypeScript",
}

_CANONICAL = {tech.lower(): tech for tech in STACK_VOCABULARY}
_CANONICAL.update(_ALIASES)

_LANGUAGE_SET = set(LANGUAGES)


def canonical(tech: str) -> str | None:
    """Приводит название к каноническому виду. None — если технология незнакома."""
    return _CANONICAL.get(tech.strip().lower())


def is_language(tech: str) -> bool:
    """Язык программирования (жёсткий критерий) или инструмент (мягкий)?"""
    return tech in _LANGUAGE_SET


def languages_in(stack: list[str]) -> set[str]:
    """Языки из стека вакансии. Пустое множество — язык в посте не назван."""
    return {tech for tech in stack if tech in _LANGUAGE_SET}
