"""Извлечение текста, структуры и метаданных из PDF, DOCX, XLSX и HTML."""
from __future__ import annotations

from pathlib import Path

from navigator.ingestion.common import ParseError, build_fragments, to_fragments  # noqa: F401
from navigator.ingestion.metadata_extractor import extract_metadata
from navigator.ingestion.model import Parsed

SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".html": "html", ".htm": "html"}


def parse_file(path: Path | str, media_type: str | None = None, **kwargs) -> Parsed:
    p = Path(path)
    media_type = media_type or SUPPORTED.get(p.suffix.lower())
    if media_type == "pdf":
        from navigator.ingestion.pdf_parser import parse_pdf
        if "ocr" not in kwargs:
            from navigator.ingestion.ocr import make_ocr
            kwargs["ocr"] = make_ocr()
        parsed = parse_pdf(p, **kwargs)
    elif media_type == "docx":
        from navigator.ingestion.docx_parser import parse_docx
        parsed = parse_docx(p)
    elif media_type == "xlsx":
        from navigator.ingestion.xlsx_parser import parse_xlsx
        parsed = parse_xlsx(p)
    elif media_type == "html":
        from navigator.ingestion.html_parser import parse_html
        parsed = parse_html(p, **kwargs)
    else:
        raise ParseError(f"Неподдерживаемый формат файла: {p.suffix}")

    if parsed.metadata is None:
        parsed.metadata = extract_metadata(parsed.units, title_hint=parsed.title, filename=p.name)

    return parsed
