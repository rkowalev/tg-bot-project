"""
Резюме -> Criteria. Один вызов Haiku, structured output, как в enrichment.

Зачем модуль вообще: с Итерации 3 фильтр принимает Criteria параметром и не
знает, откуда они. Раньше их писали руками в config/criteria.py, теперь их
даёт резюме. Код фильтра при этом не меняется ни на строку — ради этого
разделение и делали.

Резюме парсится ОДИН раз на онбординге, результат живёт в БД. На каждый
запрос его гонять незачем — оно не меняется.

Важно про словарь: Criteria валидирует технологии против config/stack.py и
падает на незнакомых. В резюме почти наверняка будет что-то за пределами
словаря (Katalon, SoapUI, Robot Framework). Поэтому модель просят выбирать
ТОЛЬКО из списка, и на всякий случай результат ещё раз фильтруется —
незнакомое просто отбрасывается, а не роняет онбординг.
"""

import os

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from config.stack import STACK_VOCABULARY, canonical
from src.filters.criteria import Criteria
from src.models.vacancy import Grade, WorkFormat

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024


class ResumeProfile(BaseModel):
    """Что ИИ вычитывает из резюме. В Criteria превращает parse_resume."""

    stack: list[str] = Field(
        description="Технологии кандидата. ТОЛЬКО из разрешённого списка, точное написание оттуда же. Что не в списке — пропусти."
    )
    grade: Grade = Field(
        description="Грейд по опыту и формулировкам резюме. unknown — если определить нельзя."
    )
    work_formats: list[WorkFormat] = Field(
        description='Желаемые форматы работы. Пустой список, если в резюме про это ничего нет.'
    )
    min_salary: int | None = Field(
        description="Зарплатные ожидания в рублях в месяц. 270к -> 270000. null, если не указаны."
    )


def _system_prompt() -> str:
    return f"""\
Ты разбираешь резюме IT-специалиста (обычно QA/тестирование) и вытаскиваешь из \
него критерии поиска работы.

Разрешённый список технологий — выбирай ТОЛЬКО из него, слово в слово:
{", ".join(STACK_VOCABULARY)}

Правила:

1. stack — технологии, которыми кандидат ВЛАДЕЕТ. Только из списка выше. \
Если в резюме есть что-то, чего в списке нет, — просто пропусти, не выдумывай \
замену. Не тащи всё подряд: бери то, что кандидат реально указывает как свой \
рабочий инструмент, а не упоминает вскользь.

2. grade — по совокупности: суммарный опыт, уровень задач, формулировки \
("ведущий", "руководил командой"). До 1 года -> junior, 1-3 -> middle, \
3-5 -> senior, руководство командой -> lead. Не угадывай — если непонятно, unknown.

3. work_formats — что кандидат ХОЧЕТ, а не где работал раньше. Ищи явные \
формулировки ("удалённо", "готов к релокации", "только офис"). Ничего про это \
нет — пустой список.

4. min_salary — зарплатные ОЖИДАНИЯ. "от 270к" -> 270000; "270 000 руб" -> \
270000. Если указана вилка ожиданий — бери нижнюю границу. Нет — null. \
Зарплату с прошлых мест работы за ожидания НЕ считай."""


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY не найден в .env")
        _client = anthropic.AsyncAnthropic()
    return _client


class ResumeParseError(RuntimeError):
    """Онбординг должен показать человеку внятную причину, а не трейсбек."""


async def parse_resume(text: str) -> Criteria:
    if not text or not text.strip():
        raise ResumeParseError("Резюме пустое — пришли текст.")
    if len(text.strip()) < 100:
        raise ResumeParseError(
            "Текст слишком короткий для резюме. Пришли полное резюме текстом или файлом."
        )

    try:
        response = await _get_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": _system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": f"Резюме:\n\n<resume>\n{text}\n</resume>"}
            ],
            output_format=ResumeProfile,
        )
    except anthropic.APIStatusError as error:
        raise ResumeParseError(f"ИИ не ответил (код {error.status_code}). Попробуй ещё раз.")
    except anthropic.APIConnectionError:
        raise ResumeParseError("Нет связи с ИИ. Попробуй ещё раз.")
    except ValidationError:
        raise ResumeParseError("ИИ вернул неожиданный ответ. Попробуй ещё раз.")

    profile = response.parsed_output
    if profile is None:
        raise ResumeParseError("ИИ не смог разобрать резюме. Попробуй ещё раз.")

    return _to_criteria(profile)


def _to_criteria(profile: ResumeProfile) -> Criteria:
    """
    Страховка на случай, если модель всё-таки вернула технологию не из словаря:
    отбрасываем незнакомое, а не роняем онбординг ValidationError'ом.
    """
    known = [tech for tech in (canonical(t) for t in profile.stack) if tech]

    return Criteria(
        work_formats=profile.work_formats,
        min_salary=profile.min_salary,
        stack_include=known,
        stack_exclude=[],
        grades=[profile.grade] if profile.grade is not Grade.UNKNOWN else [],
    )


def describe(criteria: Criteria) -> str:
    """Человекочитаемо — это показывают на подтверждении, а не model_dump."""
    grades = ", ".join(g.value for g in criteria.grades) or "любой"
    formats = ", ".join(f.value for f in criteria.work_formats) or "любой"
    stack = ", ".join(criteria.stack_include) or "не распознан"
    salary = f"от {criteria.min_salary // 1000}к" if criteria.min_salary else "не указана"
    return (
        f"<b>Стек:</b> {stack}\n"
        f"<b>Грейд:</b> {grades}\n"
        f"<b>Формат:</b> {formats}\n"
        f"<b>Зарплата:</b> {salary}"
    )
