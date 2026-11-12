"""Подсистема хранения, очереди фоновых задач и управления документами."""
from navigator.documents.queue import JobQueue
from navigator.documents.service import DocumentService
from navigator.documents.storage import DocumentStorage

__all__ = ["DocumentStorage", "JobQueue", "DocumentService"]
