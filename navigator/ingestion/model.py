"""Результат разбора файла, общий для всех парсеров."""
from __future__ import annotations

from dataclasses import dataclass, field

from navigator.ingestion.common import FragmentDraft, Unit, build_fragments
from navigator.schemas import MetadataPatch


@dataclass
class Parsed:
    media_type: str  # pdf | docx | xlsx | html
    title: str = ""
    units: list[Unit] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)  # материал для просмотрщика источников
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False  # нечитаемый OCR, пустой документ и т.п.
    drafts: list[FragmentDraft] | None = None
    metadata: MetadataPatch | None = None
    links: list[dict] = field(default_factory=list)  # ссылки на скачиваемые документы

    diagnostics: list[dict] = field(default_factory=list)

    def build(self) -> list[FragmentDraft]:
        if self.drafts is None:
            self.drafts = build_fragments(self.units)
            for draft in self.drafts:
                for warning in draft.warnings:
                    self.needs_review = True
                    if warning not in self.warnings:
                        self.warnings.append(warning)
        return self.drafts
