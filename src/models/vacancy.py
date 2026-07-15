"""
Pydantic-модель вакансии — контракт между модулями конвейера.

Модель спроектирована богато, под будущий матчинг с резюме (Итерация 5),
хотя на Итерации 1 регулярками заполняется только часть полей — остальное
(company, точная нормализация зарплаты, точный грейд, несколько вилок)
доберёт ИИ на Итерации 2. Что не распозналось — видно в parse_flags, а не
теряется молча.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from config.currency import to_rub


class WorkFormat(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    OFFICE = "office"
    UNKNOWN = "unknown"


class Grade(str, Enum):
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"
    UNKNOWN = "unknown"


class Salary(BaseModel):
    # min_value/max_value — в ИСХОДНОЙ валюте поста, как написано у автора.
    # Для сравнений и показа берите min_rub/max_rub: они приводят к рублям.
    min_value: int | None = None
    max_value: int | None = None
    currency: str | None = None
    gross: bool | None = None
    period: str | None = None  # "month" / "hour" — на этой итерации почти всегда None
    raw: str  # исходный кусок текста, из которого разобрана зарплата

    # Конвертация живёт СВОЙСТВОМ модели, а не в вызывающем коде: зарплату
    # читают правило, промпт оценки и карточка. Забыть конвертировать хотя бы
    # в одном месте — значит сравнить 2500 USD с порогом 230000 RUB и молча
    # выкинуть вакансию. Свойство забыть нельзя.
    @property
    def min_rub(self) -> int | None:
        return to_rub(self.min_value, self.currency)

    @property
    def max_rub(self) -> int | None:
        return to_rub(self.max_value, self.currency)


class Vacancy(BaseModel):
    raw_text: str  # полный исходный текст поста — обязательный fallback
    posted_at: datetime

    title: str | None = None
    company: str | None = None
    salary: Salary | None = None

    work_format_raw: str | None = None
    work_format: WorkFormat | None = None

    grade_raw: str | None = None
    grade: Grade | None = None

    stack: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    contact: str | None = None

    # Заполняет ИИ-слой (Итерация 2), регулярки этого не умеют.
    # None = обогащение ещё не проводилось (или упало — см. parse_flags).
    is_vacancy: bool | None = None
    # Вторая и последующие вилки, если в посте их несколько (основная — в salary)
    salary_alternatives: list[str] = Field(default_factory=list)

    parse_flags: list[str] = Field(default_factory=list)
