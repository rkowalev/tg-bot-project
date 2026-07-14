"""
Достаём текст резюме из файла. Про Telegram тут ничего нет — только документы.

Резюме почти всегда PDF (выгрузка с hh.ru), поэтому .txt-only было
неработающим требованием: владелец упёрся в это первым же действием.

Отдельная беда PDF — сканы. В таком файле текста нет вообще, только картинка,
и pypdf честно вернёт пустоту. Молча выдавать "резюме пустое" нельзя: человек
должен понять, что дело в файле, а не в боте.
"""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

SUPPORTED = (".pdf", ".txt", ".md")

# Ниже этого в извлечённом PDF — почти наверняка скан: текстового слоя нет.
_SCAN_THRESHOLD = 100


class DocumentError(RuntimeError):
    """Человеку показывают текст этой ошибки, поэтому он должен быть внятным."""


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        return _from_pdf(data)
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace")

    raise DocumentError(
        f"Не читаю {name.rsplit('.', 1)[-1] if '.' in name else 'такие файлы'}. "
        f"Пришли PDF, TXT — или просто текстом в сообщении."
    )


def _from_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, OSError) as error:
        raise DocumentError(f"Не смог открыть PDF ({error}). Пришли текстом.")

    text = "\n".join(pages).strip()
    if len(text) < _SCAN_THRESHOLD:
        raise DocumentError(
            "В этом PDF нет текстового слоя — похоже, это скан или картинка. "
            "Пришли резюме текстом в сообщении или выгрузи PDF заново "
            "(например, с hh.ru — там текстовый)."
        )
    return text
