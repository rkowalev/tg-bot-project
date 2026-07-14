"""
Парсер сырого текста поста в модель Vacancy — регулярками, без ИИ.

Цель Итерации 1 — не идеальный парсинг, а рабочий костяк и понимание, где
регулярки ломаются (это станет входом для ИИ на Итерации 2). Поэтому:
- берём только «лёгкое»: хэштеги, контакт, должность, зарплату (одну вилку),
  грубый work_format/grade, стек по словарю;
- точную нормализацию зарплаты, разбор нескольких вилок, company и точный
  грейд оставляем ИИ;
- ничего не распознали — не падаем, оставляем None и пишем в parse_flags,
  чтобы было видно, что потерялось, а не терять это молча.
"""

import re
from datetime import datetime

from src.models.vacancy import Grade, Salary, Vacancy, WorkFormat

# ---------- предочистка текста ----------

_EMOJI_PLACEHOLDER_RE = re.compile(r"[🔤🔣]")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clean_text(raw_text: str) -> str:
    text = raw_text.replace("*", "")
    text = _EMOJI_PLACEHOLDER_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


# ---------- зарплата ----------

# Число: "180 000" / "210.000" (точка — разделитель разрядов, не десятичная) /
# "210к" / "213 к" / "240k". Разбор конкретного значения — в _parse_number.
_NUM = r"\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d+)?\s*(?:к|k)?"
_SEP = r"(?:[-–]|до)"
_MODIFIER = r"(?:гросс|gross|нетто|net|на\s*руки|руб\.?|₽)"

# \b на конце — иначе "Оклад" матчится внутри словоформы "оклада"
# ("доплату до оклада по больничному") и уводит поиск не туда
_SALARY_KEYWORD_RE = re.compile(
    r"\b(?:ЗП|Вилка|Уровень\s*з/?п|Оклад|Уровень\s*дохода)\b\s*[:\-]?\s*([^\n]{0,80})",
    re.IGNORECASE,
)
# (?<!\d)/(?!\d) вокруг каждого числа — не дать \d{1,3} откусить середину более
# длинного числа без разделителей (иначе "1200 до 1500" парсится как "200 до 150")
_RANGE_RE = re.compile(
    rf"(?:от\s*)?(?<!\d)({_NUM})(?!\d)\s*{_SEP}\s*(?<!\d)({_NUM})(?!\d)",
    re.IGNORECASE,
)
_ANCHORED_SINGLE_RE = re.compile(
    rf"(?:от|до)\s*(?<!\d)({_NUM})(?!\d)|(?<!\d)({_NUM})(?!\d)\s*{_MODIFIER}",
    re.IGNORECASE,
)
_GROSS_RE = re.compile(r"гросс|gross", re.IGNORECASE)
_NET_RE = re.compile(r"нетто|net|на\s*руки", re.IGNORECASE)
_CURRENCY_RUB_RE = re.compile(r"₽|руб", re.IGNORECASE)


def _parse_number(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None

    # "210.000" — точка как разделитель разрядов, не десятичная дробь
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", token):
        return int(token.replace(".", ""))

    # "210к" / "213 к" / "240k" — суффикс тысяч
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*[кk]", token, re.IGNORECASE)
    if match:
        value = float(match.group(1).replace(",", "."))
        return int(value * 1000)

    digits_only = token.replace(" ", "").replace(" ", "")
    if not digits_only.isdigit():
        return None

    # "180 000" — пробел как разделитель разрядов, значение уже полное
    if " " in token or " " in token:
        return int(digits_only)

    # голое число без суффикса и разделителей ("102", "150") — в контексте
    # зарплаты в этих постах это всегда тысячи
    value = int(digits_only)
    return value * 1000 if value < 1000 else value


def _search_salary_match(text: str) -> re.Match | None:
    return _RANGE_RE.search(text) or _ANCHORED_SINGLE_RE.search(text)


def _extract_salary(text: str) -> tuple[Salary | None, list[str]]:
    flags: list[str] = []

    source = text
    keyword_match = _SALARY_KEYWORD_RE.search(text)
    match = None
    if keyword_match:
        match = _search_salary_match(keyword_match.group(1))
        if match:
            source = keyword_match.group(1)
    if match is None:
        # без опорного ключевого слова ищем по всему тексту только вилку
        # (два числа с разделителем) — одиночное число вида "до 10" слишком
        # легко ловит что-то не про зарплату (например, "до 10 дней отпуска")
        match = _RANGE_RE.search(text)
        source = text

    if match is None:
        return None, flags

    if match.re is _RANGE_RE:
        min_value = _parse_number(match.group(1))
        max_value = _parse_number(match.group(2))
    else:
        num = match.group(1) or match.group(2)
        min_value = max_value = _parse_number(num)

    if min_value is None and max_value is None:
        flags.append("salary_range_not_parsed")

    context = source[max(0, match.start() - 10) : match.end() + 20]
    gross: bool | None = None
    if _GROSS_RE.search(context):
        gross = True
    elif _NET_RE.search(context):
        gross = False
    currency = "RUB" if _CURRENCY_RUB_RE.search(context) else None

    salary = Salary(
        min_value=min_value,
        max_value=max_value,
        currency=currency,
        gross=gross,
        period=None,
        raw=match.group(0).strip(),
    )

    # вторая вилка в посте — не разбираем полностью, просто помечаем как есть
    extra = _search_salary_match(source[match.end() :])
    if extra:
        flags.append(f"additional_salary_fork: {extra.group(0).strip()}")

    return salary, flags


# ---------- должность ----------

# (?<!#) — не дать сработать на хэштеге "#вакансия" в шапке поста раньше,
# чем на настоящей строке "Вакансия: <должность>"
_TITLE_RE = re.compile(
    r"(?<!#)(?:Должность|Вакансия)\s*:?\s*\n?\s*([^\n]+)", re.IGNORECASE
)


def _extract_title(text: str) -> str | None:
    match = _TITLE_RE.search(text)
    if not match:
        return None
    title = match.group(1).strip(" :-—")
    return title or None


# ---------- контакт ----------

_CONTACT_AT_RE = re.compile(r"@\w+")
_CONTACT_LABELED_RE = re.compile(
    r"(?:резюме|контакт|писать|пишите)[:\s]*([A-Za-z][A-Za-z0-9_]{2,31})",
    re.IGNORECASE,
)
_CONTACT_URL_RE = re.compile(r"https?://\S+")


def _extract_contact(text: str) -> str | None:
    match = _CONTACT_AT_RE.search(text)
    if match:
        return match.group(0)

    match = _CONTACT_LABELED_RE.search(text)
    if match:
        return match.group(1)

    match = _CONTACT_URL_RE.search(text)
    if match:
        return match.group(0)

    return None


# ---------- хэштеги ----------

_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)


def _extract_hashtags(text: str) -> list[str]:
    return _HASHTAG_RE.findall(text)


# ---------- work_format / grade — грубый маппинг по словам ----------

_WORK_FORMAT_PATTERNS: list[tuple[re.Pattern, WorkFormat]] = [
    (re.compile(r"удал\w*|remote|дистанц\w*", re.IGNORECASE), WorkFormat.REMOTE),
    (re.compile(r"гибрид\w*|hybrid", re.IGNORECASE), WorkFormat.HYBRID),
    (re.compile(r"офис\w*|очно|office", re.IGNORECASE), WorkFormat.OFFICE),
]

_GRADE_PATTERNS: list[tuple[re.Pattern, Grade]] = [
    (re.compile(r"senior|сеньор|синьор", re.IGNORECASE), Grade.SENIOR),
    (re.compile(r"middle|мидл|миддл", re.IGNORECASE), Grade.MIDDLE),
    (re.compile(r"junior|джуниор|джун\w*", re.IGNORECASE), Grade.JUNIOR),
    (re.compile(r"lead|лид|тимлид", re.IGNORECASE), Grade.LEAD),
]


def _extract_work_format(text: str) -> tuple[str | None, WorkFormat]:
    for line in text.splitlines():
        for pattern, work_format in _WORK_FORMAT_PATTERNS:
            if pattern.search(line):
                return line.strip(), work_format
    return None, WorkFormat.UNKNOWN


def _extract_grade(text: str) -> tuple[str | None, Grade]:
    for line in text.splitlines():
        for pattern, grade in _GRADE_PATTERNS:
            if pattern.search(line):
                return line.strip(), grade
    return None, Grade.UNKNOWN


# ---------- стек — по словарю технологий ----------

_STACK_DICTIONARY = [
    "Python", "Java", "Kotlin", "Swift", "Playwright", "Selenium", "Appium",
    "SQL", "Postman", "Docker", "CI/CD", "Jenkins", "Git", "Jira", "Allure",
    "REST", "Swagger", "Charles", "JMeter", "k6", "PostgreSQL", "MySQL",
    "MongoDB", "Linux", "Bash", "JavaScript", "TypeScript", "Cypress",
    "TestRail", "Grafana", "Kubernetes",
]  # noqa: E501


def _extract_stack(text: str) -> list[str]:
    found = []
    for tech in _STACK_DICTIONARY:
        pattern = re.compile(rf"(?<!\w){re.escape(tech)}(?!\w)", re.IGNORECASE)
        if pattern.search(text):
            found.append(tech)
    return found


# ---------- сборка модели ----------


def parse_vacancy(raw_text: str, posted_at: datetime) -> Vacancy:
    flags: list[str] = []
    text = _clean_text(raw_text)

    title = _extract_title(text)
    if title is None:
        flags.append("title_not_parsed")

    salary, salary_flags = _extract_salary(text)
    flags.extend(salary_flags)
    if salary is None:
        flags.append("salary_not_parsed")

    work_format_raw, work_format = _extract_work_format(text)
    if work_format is WorkFormat.UNKNOWN:
        flags.append("work_format_not_parsed")

    grade_raw, grade = _extract_grade(text)
    if grade is Grade.UNKNOWN:
        flags.append("grade_not_parsed")

    contact = _extract_contact(text)
    if contact is None:
        flags.append("contact_not_parsed")

    return Vacancy(
        raw_text=raw_text,
        posted_at=posted_at,
        title=title,
        company=None,
        salary=salary,
        work_format_raw=work_format_raw,
        work_format=work_format,
        grade_raw=grade_raw,
        grade=grade,
        stack=_extract_stack(text),
        hashtags=_extract_hashtags(text),
        contact=contact,
        parse_flags=flags,
    )
