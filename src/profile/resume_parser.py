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

from config.stack import LANGUAGES, TOOLS, canonical, is_language
from src.filters.criteria import Criteria
from src.models.vacancy import Grade, WorkFormat

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024


class ResumeProfile(BaseModel):
    """Что ИИ вычитывает из резюме. В Criteria превращает parse_resume."""

    languages: list[str] = Field(
        description="Языки программирования, на которых кандидат РЕАЛЬНО пишет. Только из списка языков. Обычно один-два — не тащи те, что упомянуты вскользь."
    )
    tools: list[str] = Field(
        description="Инструменты и фреймворки кандидата. Только из списка инструментов. Что не в списке — пропусти."
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

Языки программирования — выбирай ТОЛЬКО отсюда, слово в слово:
{", ".join(LANGUAGES)}

Инструменты и фреймворки — ТОЛЬКО отсюда, слово в слово:
{", ".join(TOOLS)}

Правила:

1. languages — языки, на которых кандидат РЕАЛЬНО пишет код. Обычно один-два. \
Это самый важный пункт: по нему потом жёстко отсекаются вакансии на чужих \
языках. Не тащи язык, упомянутый вскользь ("видел Java у коллег") или из \
давнего прошлого — только то, на чём кандидат работает.

2. tools — инструменты и фреймворки, которыми кандидат владеет. Не из списка — \
пропусти, не выдумывай замену. Не тащи всё подряд.

3. grade — по совокупности: суммарный опыт, уровень задач, формулировки \
("ведущий", "руководил командой"). До 1 года -> junior, 1-3 -> middle, \
3-5 -> senior, руководство командой -> lead. Не угадывай — если непонятно, unknown.

4. work_formats — что кандидат ХОЧЕТ, а не где работал раньше. Ищи явные \
формулировки ("удалённо", "готов к релокации", "только офис"). Ничего про это \
нет — пустой список.

5. min_salary — зарплатные ОЖИДАНИЯ. "от 270к" -> 270000; "270 000 руб" -> \
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
    Страховка на случай, если модель вернула технологию не из словаря:
    отбрасываем незнакомое, а не роняем онбординг ValidationError'ом.

    Грейд в критерии НЕ переносим. Резюме говорит "я senior" — это факт о
    кандидате, а не требование к вакансии. Вакансия на middle с зарплатой выше
    порога вполне устраивает, и отсекать её по грейду — терять деньги.
    Реальный фильтр здесь — зарплата, а грейд пусть учитывает ИИ-оценка.
    """
    languages = [
        tech
        for tech in (canonical(t) for t in profile.languages)
        if tech and is_language(tech)
    ]
    tools = [
        tech
        for tech in (canonical(t) for t in profile.tools)
        if tech and not is_language(tech)
    ]

    return Criteria(
        work_formats=profile.work_formats,
        min_salary=profile.min_salary,
        languages=languages,
        stack_include=tools,
        stack_exclude=[],
        grades=[],
    )


_FORMAT_RU = {
    WorkFormat.REMOTE: "удалёнка",
    WorkFormat.HYBRID: "гибрид",
    WorkFormat.OFFICE: "офис",
    WorkFormat.UNKNOWN: "не указан",
}


def describe(criteria: Criteria) -> str:
    """Человекочитаемо — это показывают на подтверждении, а не model_dump."""
    languages = ", ".join(criteria.languages) or "не распознан"
    formats = ", ".join(_FORMAT_RU[f] for f in criteria.work_formats) or "любой"
    tools = ", ".join(criteria.stack_include) or "не распознаны"
    salary = f"от {criteria.min_salary // 1000}к" if criteria.min_salary else "не указана"
    lines = [
        f"<b>Язык:</b> {languages}  <i>(вакансии на других языках отсекаю)</i>",
        f"<b>Зарплата:</b> {salary}",
        f"<b>Формат:</b> {formats}",
        f"<b>Инструменты:</b> {tools}  <i>(не отсекаю — их можно доучить)</i>",
    ]
    if criteria.grades:
        lines.append(f"<b>Грейд:</b> {', '.join(g.value for g in criteria.grades)}")
    return "\n".join(lines)
