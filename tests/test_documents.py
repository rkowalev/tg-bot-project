"""
Тесты чтения резюме из файлов. Настоящие PDF, а не моки.
"""

import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from src.profile.documents import DocumentError, extract_text

RESUME_LINES = [
    "Kovalev Ruslan",
    "QA Automation Engineer",
    "Zhelaemaya zarplata: ot 270 000 rub.",
    "Format raboty: udalenno",
    "Opyt: 6 let. Python, pytest, Playwright, Selenium.",
    "Testirovanie REST API, rabota s PostgreSQL, Docker, Jenkins.",
]


def _pdf_with_text(lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in lines:
        pdf.drawString(50, y, line)
        y -= 18
    pdf.save()
    return buffer.getvalue()


def _pdf_without_text() -> bytes:
    """Скан: страница есть, текстового слоя нет."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.rect(50, 700, 200, 100, fill=1)
    pdf.save()
    return buffer.getvalue()


# ---------- PDF: то, чего не хватало владельцу ----------


def test_pdf_text_is_extracted():
    text = extract_text("resume.pdf", _pdf_with_text(RESUME_LINES))
    assert "Kovalev Ruslan" in text
    assert "Python" in text
    assert "270 000" in text


def test_pdf_keeps_all_pages():
    text = extract_text("resume.pdf", _pdf_with_text(RESUME_LINES * 30))
    assert text.count("Kovalev Ruslan") == 30, "многостраничное резюме не должно обрезаться"


def test_uppercase_extension_works():
    text = extract_text("RESUME.PDF", _pdf_with_text(RESUME_LINES))
    assert "Kovalev Ruslan" in text


# ---------- понятные ошибки вместо тишины ----------


def test_scanned_pdf_says_it_is_a_scan():
    """Скан молча выглядит как пустое резюме — человек должен знать причину."""
    with pytest.raises(DocumentError, match="нет текстового слоя"):
        extract_text("scan.pdf", _pdf_without_text())


def test_broken_pdf_does_not_leak_traceback():
    with pytest.raises(DocumentError, match="Не смог открыть PDF"):
        extract_text("resume.pdf", b"this is not a pdf at all")


def test_unsupported_extension_names_it():
    with pytest.raises(DocumentError, match="docx"):
        extract_text("resume.docx", b"PK\x03\x04")


def test_file_without_extension():
    with pytest.raises(DocumentError, match="такие файлы"):
        extract_text("resume", b"data")


# ---------- txt ----------


def test_txt_is_read():
    text = extract_text("resume.txt", "Резюме: Python, pytest".encode("utf-8"))
    assert text == "Резюме: Python, pytest"


def test_md_is_read():
    text = extract_text("resume.md", "# Резюме\nPython".encode("utf-8"))
    assert "Python" in text


def test_broken_encoding_does_not_crash():
    text = extract_text("resume.txt", b"\xff\xfe Python")
    assert "Python" in text, "битые байты заменяем, а не падаем"
