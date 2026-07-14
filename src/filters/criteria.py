"""
Criteria — что пользователь ищет. Контракт входа фильтра.

Ключевое: фильтр НЕ знает, откуда критерии взялись. Сейчас их задаёт руками
config/criteria.py, на Итерации 5 часть полей заполнит парсер резюме, на
Итерации 3-бота — FSM-диалог. Поэтому фильтр принимает Criteria параметром и
ничего не хардкодит.

Все поля опциональны: пустое = критерий не применяется.

Важно про Итерацию 5: резюме отвечает не на все поля. Из резюме выводимы
stack_include и grades ("что я умею"), но min_salary и work_formats — это
предпочтения, их в резюме нет. Так что парсер резюме заполнит Criteria
частично, а остальное всё равно придёт от пользователя.
"""

from pydantic import BaseModel, Field, field_validator

from config.stack import STACK_VOCABULARY, canonical
from src.models.vacancy import Grade, WorkFormat


class Criteria(BaseModel):
    work_formats: list[WorkFormat] = Field(default_factory=list)
    min_salary: int | None = None
    stack_include: list[str] = Field(default_factory=list)  # желаемые технологии
    stack_exclude: list[str] = Field(default_factory=list)  # стоп-технологии
    grades: list[Grade] = Field(default_factory=list)

    @field_validator("stack_include", "stack_exclude")
    @classmethod
    def _known_technologies(cls, values: list[str]) -> list[str]:
        """
        Технологию, которой нет в словаре, парсер извлечь не сможет — критерий
        по ней молча не сматчится никогда. Поэтому падаем сразу, а не удивляемся
        пустой выдаче. Заодно приводим к каноническому виду.
        """
        result = []
        for value in values:
            name = canonical(value)
            if name is None:
                raise ValueError(
                    f"технология {value!r} не в config/stack.py — парсер её не "
                    f"извлекает, критерий не сматчится. Добавь в STACK_VOCABULARY "
                    f"или убери. Известные: {', '.join(sorted(STACK_VOCABULARY))}"
                )
            result.append(name)
        return result
