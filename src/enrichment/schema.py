"""
Схема ответа ИИ — контракт с моделью, отдельный от контракта между модулями.

Почему не отдаём ИИ саму Vacancy: модель отвечает не за всю вакансию, а только
за поля, которые не взяли регулярки. raw_text/posted_at/hashtags она не должна
ни видеть в схеме ответа, ни выдумывать. Эта схема уходит в structured output,
поэтому ответ гарантированно валиден и кладётся в Vacancy без ручной чистки.

Все поля обязательные и nullable (без значений по умолчанию) — так требует
structured output: в JSON Schema каждое поле попадает в required, а "не знаю"
модель выражает через null, а не через отсутствие ключа.
"""

from typing import Literal

from pydantic import BaseModel, Field

from src.models.vacancy import Grade


class EnrichedSalary(BaseModel):
    min_value: int | None = Field(
        description="Нижняя граница в рублях за период. 160к -> 160000. Если вилки нет, а есть одно число — оно и в min, и в max."
    )
    max_value: int | None = Field(description="Верхняя граница в рублях за период.")
    currency: str | None = Field(description='Код валюты: "RUB", "USD", "EUR".')
    gross: bool | None = Field(
        description="true = до вычета налогов (гросс), false = на руки (net/нетто). null, если в посте не сказано."
    )
    period: Literal["month", "hour"] | None = Field(
        description='Период выплаты. "hour" только если в посте явно про ставку в час.'
    )
    raw: str | None = Field(
        description="Точная подстрока из поста, откуда взята эта зарплата, как есть."
    )


class EnrichmentResult(BaseModel):
    is_vacancy: bool = Field(
        description="true — пост является вакансией. false — реклама, флуд, служебное сообщение модератора, обсуждение."
    )
    title: str | None = Field(
        description="Должность одной строкой, без компании и зарплаты. null, если пост не вакансия."
    )
    company: str | None = Field(
        description="Название компании или кадрового агентства. null, если не указано."
    )
    grade: Grade = Field(
        description="Грейд по совокупности: требуемый опыт в годах, формулировки, заголовок. unknown — если определить нельзя."
    )
    salary: EnrichedSalary | None = Field(
        description="Основная вилка: месячная по ТК, если есть выбор. null, если зарплаты в посте нет."
    )
    salary_alternatives: list[str] = Field(
        description="Остальные вилки как подстроки из поста (ИП/час/другой формат оформления). Пустой список, если вилка одна."
    )
