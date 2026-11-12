"""Извлечение текста и структуры из PDF, DOCX, XLSX (HTML — отдельным парсером)."""
from __future__ import annotations

from pathlib import Path

from navigator.ingestion.common import ParseError, build_fragments, to_fragments  # noqa: F401
from navigator.ingestion.model import Parsed

SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".html": "html", ".htm": "html"}


def parse_file(path: Path, media_type: str | None = None, **kwargs) -> Parsed:
    media_type = media_type or SUPPORTED.get(Path(path).suffix.lower())
    if media_type == "pdf":
        from navigator.ingestion.pdf_parser import parse_pdf
        if "ocr" not in kwargs:
            from navigator.ingestion.ocr import make_ocr
            kwargs["ocr"] = make_ocr()
        return parse_pdf(Path(path), **kwargs)
    if media_type == "docx":
        from navigator.ingestion.docx_parser import parse_docx
        return parse_docx(Path(path))
    if media_type == "xlsx":
        from navigator.ingestion.xlsx_parser import parse_xlsx
        return parse_xlsx(Path(path))
    raise ParseError("Неподдерживаемый формат файла")
