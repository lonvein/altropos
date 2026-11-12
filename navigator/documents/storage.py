"""Менеджер хранения файлов: оригиналы в runtime/originals, производные в runtime/extracted.

Безопасность:
- Имена пользователей не используются как имена файлов на диске: генерируется безопасный ID.
- Проверка размера (MAX_UPLOAD_MB) и разрешённых расширений (.pdf, .docx, .xlsx, .html, .htm).
- Защита от Path Traversal (_safe_path): путь обязан оставаться внутри корня хранилища.
- Дедупликация по SHA-256: вычисление хеша до сохранения.
"""
from __future__ import annotations

import json
from pathlib import Path

from navigator.ingestion.common import ParseError, document_ids, sha256_bytes

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".html", ".htm"}
DEFAULT_MAX_UPLOAD_MB = 40


class DocumentStorage:
    def __init__(self, root: Path | str, max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB):
        self.root = Path(root).resolve()
        self.originals = self.root / "originals"
        self.extracted = self.root / "extracted"
        self.max_bytes = max_upload_mb * 1024 * 1024

        for folder in (self.root, self.originals, self.extracted):
            folder.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, relative_path: Path | str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Недопустимый путь к файлу: выход за пределы хранилища")
        return target

    def validate_file(self, filename: str, content: bytes) -> tuple[str, str]:
        """Проверяет расширение и размер файла; возвращает (ext, media_type)."""
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ParseError(f"Неподдерживаемый формат файла: '{ext}'. Разрешены: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")

        if len(content) > self.max_bytes:
            raise ParseError(f"Превышен максимальный размер файла ({len(content) // (1024*1024)} МБ > {self.max_bytes // (1024*1024)} МБ)")

        media_type = "html" if ext in (".html", ".htm") else ext.lstrip(".")
        return ext, media_type

    def save_original(self, filename: str, content: bytes) -> tuple[str, str, str, str, str]:
        """Сохраняет оригинал под безопасным ID.

        Возвращает: (document_id, version, relative_path, sha256, media_type).
        """
        ext, media_type = self.validate_file(filename, content)
        sha = sha256_bytes(content)
        doc_id, version = document_ids(sha)

        safe_name = f"{doc_id}{ext}"
        rel_path = f"originals/{safe_name}"
        target = self._safe_path(rel_path)

        if not target.exists():
            target.write_bytes(content)

        return doc_id, version, rel_path, sha, media_type

    def get_original_path(self, relative_path: str) -> Path:
        target = self._safe_path(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"Файл оригинала не найден: {relative_path}")
        return target

    def save_extracted_blocks(self, doc_id: str, blocks: list[dict]) -> str:
        """Сохраняет производные блоки для быстрого рендеринга просмотра источников."""
        rel_path = f"extracted/{doc_id}.blocks.json"
        target = self._safe_path(rel_path)
        target.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
        return rel_path

    def get_extracted_blocks(self, doc_id: str) -> list[dict] | None:
        rel_path = f"extracted/{doc_id}.blocks.json"
        target = self._safe_path(rel_path)
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
