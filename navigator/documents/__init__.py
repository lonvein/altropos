"""Подсистема хранения, очереди фоновых задач и управления документами."""
from navigator.documents.queue import JobQueue
from navigator.documents.service import DocumentService
from navigator.documents.site_importer import (
    fetch_mipt_bytes,
    inspect_mipt_page,
    is_mipt_host,
    validate_mipt_url,
)
from navigator.documents.storage import DocumentStorage
from navigator.documents.viewer import format_locator_human, render_source_html

__all__ = [
    "DocumentStorage",
    "JobQueue",
    "DocumentService",
    "format_locator_human",
    "render_source_html",
    "is_mipt_host",
    "validate_mipt_url",
    "fetch_mipt_bytes",
    "inspect_mipt_page",
]
