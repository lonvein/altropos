from __future__ import annotations

import hashlib
import html
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from navigator.config import Settings
from navigator.schemas import (
    AskRequest,
    AskResponse,
    Claim,
    Evidence,
    ImportRequest,
    MetadataPatch,
    Source,
    Verification,
)

DEMO_WARNING = (
    "ДЕМО-ЗАГЛУШКА: данные искусственные. Поиск, извлечение текста и языковая "
    "модель не выполнялись; ответ нельзя использовать как настоящий результат."
)
DEMO_DOCUMENT_WARNING = (
    "ДЕМО-ЗАГЛУШКА: файл не обрабатывался. Статус, фрагмент и источник созданы "
    "искусственно для проверки интерфейса."
)

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html; charset=utf-8",
}
TERMINAL_JOB_STATUSES = {"ready", "needs_review", "error", "failed"}
BUILTIN_DOCUMENT_ID = "demo-doc-regulation"
BUILTIN_FRAGMENT_ID = "demo-doc-regulation-f1"
BUILTIN_FRAGMENT_2_ID = "demo-doc-regulation-f2"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_title(value: str, fallback: str = "Демонстрационный документ") -> str:
    title = " ".join((value or "").strip().split())
    return title[:500] or fallback


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    message = str(first.get("msg") or "Некорректные данные")
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            message = message[len(prefix):]
    lowered = message.lower()
    if "at least 1 character" in lowered or "at least 3 characters" in lowered:
        return "Введите содержательный вопрос (не менее 3 символов)."
    if "string should have at most" in lowered:
        return "Значение слишком длинное."
    return message


def _locator_label(locator: dict[str, Any]) -> str:
    kind = locator.get("kind")
    if kind == "pdf":
        page = locator.get("page")
        return f"PDF, страница {page}" if page else "PDF"
    if kind == "docx":
        return f"DOCX, блок {int(locator.get('block', 0)) + 1}"
    if kind == "xlsx":
        sheet = locator.get("sheet", "")
        cell_range = locator.get("range", "")
        return f"XLSX, лист «{sheet}», диапазон {cell_range}"
    if kind == "html":
        return f"HTML, блок {int(locator.get('block', 0)) + 1}"
    return "Источник"


def _locator_for(media_type: str) -> dict[str, Any]:
    if media_type == "pdf":
        return {"kind": "pdf", "page": 1}
    if media_type == "docx":
        return {"kind": "docx", "block": 0}
    if media_type == "xlsx":
        return {"kind": "xlsx", "sheet": "Демо", "range": "A1:B2"}
    return {"kind": "html", "block": 0}


def _builtin_html() -> bytes:
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Искусственный демонстрационный регламент</title></head><body>"
        "<article><h1>Искусственный демонстрационный регламент</h1>"
        "<p>ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.</p>"
        "<p>ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору не позднее чем за два дня.</p>"
        "<p>Документ создан заглушкой интерфейса и не является регламентом организации.</p>"
        "</article></body></html>"
    ).encode("utf-8")


@dataclass
class _Document:
    id: str
    title: str
    name: str
    media_type: str
    status: str
    warnings: list[str]
    metadata: dict[str, Any]
    sha256: str
    content: bytes
    created: str
    version: str = "demo-v1"
    fragment_id: str = ""
    source_url: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "title": self.title,
            "sha256": self.sha256,
            "original_path": f"demo/{self.id}",
            "media_type": self.media_type,
            "status": self.status,
            "source_url": self.source_url,
            "retrieved_at": None,
            "metadata": dict(self.metadata),
            "warnings": list(self.warnings),
            "created": self.created,
            "fragment_id": self.fragment_id or None,
            "original_available": bool(self.content),
        }


@dataclass
class _Job:
    id: str
    kind: str
    created: float
    delay: float
    final_status: str
    payload: dict[str, Any]
    status: str = "queued"
    error: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class _Fragment:
    id: str
    document_id: str
    text: str
    locator: dict[str, Any]
    ordinal: int = 0


class SyntheticState:
    """Small in-memory fixture store used only by the marked fallback router."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.documents: dict[str, _Document] = {}
        self.jobs: dict[str, _Job] = {}
        self.fragments: dict[str, _Fragment] = {}
        self._create_builtin()

    # ------------------------------------------------------------------ setup
    def _create_builtin(self) -> None:
        content = _builtin_html()
        doc = _Document(
            id=BUILTIN_DOCUMENT_ID,
            title="Искусственный демонстрационный регламент",
            name="demo-regulation.html",
            media_type="html",
            status="ready",
            warnings=[DEMO_DOCUMENT_WARNING],
            metadata={
                "title": "Искусственный демонстрационный регламент",
                "number": "ДЕМО-001",
                "issued": "2026-01-01",
                "valid_from": "2026-01-01",
                "valid_to": None,
                "reviewed": True,
                "relations": [],
            },
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            created=_now_iso(),
            fragment_id=BUILTIN_FRAGMENT_ID,
        )
        self.documents[doc.id] = doc
        self._add_fragment(
            doc,
            BUILTIN_FRAGMENT_ID,
            "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
            {"kind": "html", "block": 0},
            0,
        )
        self._add_fragment(
            doc,
            BUILTIN_FRAGMENT_2_ID,
            "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору не позднее чем за два дня.",
            {"kind": "html", "block": 1},
            1,
        )

    def _add_fragment(
        self,
        document: _Document,
        fragment_id: str,
        text: str,
        locator: dict[str, Any],
        ordinal: int,
    ) -> None:
        self.fragments[fragment_id] = _Fragment(
            id=fragment_id,
            document_id=document.id,
            text=text,
            locator=locator,
            ordinal=ordinal,
        )

    def _new_document(
        self,
        *,
        doc_id: str,
        title: str,
        name: str,
        media_type: str,
        status: str,
        content: bytes,
        warnings: list[str],
        source_url: str | None = None,
    ) -> _Document:
        fragment_id = f"{doc_id}-f1"
        locator = _locator_for(media_type)
        text = f"ДЕМО-ФРАГМЕНТ. Документ «{_clean_title(title)}» принят демонстрационной очередью."
        doc = _Document(
            id=doc_id,
            title=_clean_title(title),
            name=name,
            media_type=media_type,
            status=status,
            warnings=list(warnings),
            metadata={
                "title": _clean_title(title),
                "number": None,
                "issued": None,
                "valid_from": None,
                "valid_to": None,
                "reviewed": None,
                "relations": [],
            },
            sha256=hashlib.sha256(content).hexdigest() if content else hashlib.sha256(doc_id.encode()).hexdigest(),
            content=content,
            created=_now_iso(),
            fragment_id=fragment_id,
            source_url=source_url,
        )
        self.documents[doc.id] = doc
        self._add_fragment(doc, fragment_id, text, locator, 0)
        return doc

    def _new_job(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        delay: float,
        final_status: str = "ready",
    ) -> _Job:
        job = _Job(
            id=f"demo-job-{uuid4().hex[:12]}",
            kind=kind,
            created=time.monotonic(),
            delay=delay,
            final_status=final_status,
            payload=payload,
        )
        self.jobs[job.id] = job
        return job

    # ---------------------------------------------------------------- uploads
    async def save_upload(self, file: UploadFile) -> tuple[_Document, _Job, bool]:
        filename = Path(file.filename or "").name
        extension = Path(filename).suffix.lower()
        media_type = ALLOWED_EXTENSIONS.get(extension)
        if media_type is None:
            raise HTTPException(
                status_code=400,
                detail="Поддерживаются только файлы PDF, DOCX и XLSX.",
            )

        limit = self.settings.max_upload_mb * 1024 * 1024
        content = await file.read(limit + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Файл пустой.")
        if len(content) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"Размер файла превышает {self.settings.max_upload_mb} МБ.",
            )

        digest = hashlib.sha256(content).hexdigest()
        for existing in self.documents.values():
            if existing.sha256 == digest and existing.content:
                job = self._new_job(
                    "document",
                    {"document_id": existing.id, "duplicate": True},
                    delay=0.0,
                    final_status=existing.status,
                )
                return existing, job, True

        title = _clean_title(Path(filename).stem)
        lowered = title.lower()
        if "review" in lowered or "проверк" in lowered:
            status = "needs_review"
        elif "error" in lowered or "ошиб" in lowered or "сломан" in lowered:
            status = "error"
        else:
            status = "ready"

        warnings = [DEMO_DOCUMENT_WARNING]
        if status == "needs_review":
            warnings.append("ДЕМО: документ помечен как требующий проверки администратора.")
        elif status == "error":
            warnings.append("ДЕМО: искусственная ошибка обработки файла.")

        doc = self._new_document(
            doc_id=f"demo-doc-{uuid4().hex[:16]}",
            title=title,
            name=filename or "document",
            media_type=media_type,
            status=status,
            content=content,
            warnings=warnings,
        )
        job = self._new_job(
            "document",
            {"document_id": doc.id, "duplicate": False},
            delay=1.0,
            final_status=status,
        )
        return doc, job, False

    # ------------------------------------------------------------------ jobs
    def job_public(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        self._advance(job)
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "demo": True,
        }

    def _advance(self, job: _Job) -> None:
        if job.status in TERMINAL_JOB_STATUSES:
            return
        elapsed = time.monotonic() - job.created
        if elapsed < 0.15:
            job.status = "queued"
            return
        if elapsed < job.delay:
            job.status = "processing"
            return
        try:
            self._finish(job)
        except Exception:  # noqa: BLE001 - a fake job must not crash the poller
            job.status = "error"
            job.error = "ДЕМО: искусственная ошибка задания."

    def _finish(self, job: _Job) -> None:
        if job.kind == "document":
            document = self.documents.get(job.payload["document_id"])
            if document is None:
                job.status = "error"
                job.error = "Демонстрационный документ не найден."
                return
            document.status = job.final_status
            job.status = job.final_status
            if job.final_status in {"error", "failed"}:
                job.error = "ДЕМО: обработка завершилась ошибкой."
            job.result = {
                "document_id": document.id,
                "status": document.status,
                "duplicate": bool(job.payload.get("duplicate")),
            }
            return

        if job.kind == "import_preview":
            job.status = "ready"
            job.result = job.payload["preview"]
            return

        if job.kind == "import":
            url = job.payload["url"]
            preview = self._preview(url)
            allowed = {link["url"]: link for link in preview["links"] if link.get("allowed")}
            created: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for selected in job.payload.get("selected_urls", []):
                link = allowed.get(selected)
                if link is None:
                    rejected.append({"url": selected, "reason": "Ссылка не входит в разрешённый список."})
                    continue
                extension = Path(urlparse(selected).path).suffix.lower()
                media_type = ALLOWED_EXTENSIONS.get(extension)
                if media_type is None:
                    rejected.append({"url": selected, "reason": "Формат файла не поддерживается."})
                    continue
                name = Path(urlparse(selected).path).name or "imported-document"
                doc = self._new_document(
                    doc_id=f"demo-doc-{uuid4().hex[:16]}",
                    title=link.get("title") or name,
                    name=name,
                    media_type=media_type,
                    status="ready",
                    content=b"",
                    warnings=[
                        DEMO_DOCUMENT_WARNING,
                        "ДЕМО: импорт по URL не сохранил оригинал; доступно только искусственное представление.",
                    ],
                    source_url=selected,
                )
                created.append(doc.public())
            job.status = "ready"
            job.result = {
                "url": url,
                "selected_urls": job.payload.get("selected_urls", []),
                "documents": created,
                "rejected": rejected,
            }
            return

        job.status = "error"
        job.error = "Неизвестный тип демонстрационного задания."

    # ---------------------------------------------------------------- import
    def _check_url(self, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=400, detail="Укажите корректный URL http(s).")
        if parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="URL с логином или паролем не разрешён.")
        host = parsed.hostname.lower()
        if host != "mipt.ru" and not host.endswith(".mipt.ru"):
            raise HTTPException(status_code=400, detail="Разрешены только URL сайта mipt.ru и его поддоменов.")
        return value.strip()

    def _preview(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}/__demo__"
        links = [
            {
                "url": f"{base}/iskusstvennyy-prikaz.pdf",
                "title": "ИСКУССТВЕННЫЙ ПРИМЕР: приказ (PDF)",
                "kind": "pdf",
                "allowed": True,
            },
            {
                "url": f"{base}/iskusstvennoe-polozhenie.docx",
                "title": "ИСКУССТВЕННЫЙ ПРИМЕР: положение (DOCX)",
                "kind": "docx",
                "allowed": True,
            },
            {
                "url": f"{base}/iskusstvennaya-tablica.xlsx",
                "title": "ИСКУССТВЕННЫЙ ПРИМЕР: таблица (XLSX)",
                "kind": "xlsx",
                "allowed": True,
            },
        ]
        rejected = [
            {
                "url": "https://example.org/ne-dopuskaetsya.pdf",
                "title": "Недопустимый внешний пример",
                "reason": "Домен не относится к mipt.ru.",
            }
        ]
        return {
            "url": url,
            "page_title": "ИСКУССТВЕННАЯ ДЕМО-СТРАНИЦА",
            "page_excerpt": (
                "Это демонстрационный ответ заглушки. Настоящая страница сайта не "
                "загружалась, ссылки ниже искусственные."
            ),
            "links": links,
            "rejected": rejected,
            "demo": True,
        }

    def import_preview(self, url: str) -> _Job:
        return self._new_job(
            "import_preview",
            {"preview": self._preview(url)},
            delay=0.5,
            final_status="ready",
        )

    def import_selected(self, url: str, selected_urls: list[str]) -> _Job:
        return self._new_job(
            "import",
            {"url": url, "selected_urls": selected_urls},
            delay=0.8,
            final_status="ready",
        )

    # ------------------------------------------------------------------- ask
    def _latest_ready(self) -> _Document:
        # Insertion order is intentional: the built-in fixture is created first,
        # so the most recently uploaded/imported ready document wins.
        for document in reversed(list(self.documents.values())):
            if document.status == "ready":
                return document
        return self.documents[BUILTIN_DOCUMENT_ID]

    def _source(self, fragment: _Fragment) -> Source:
        document = self.documents[fragment.document_id]
        return Source(
            fragment_id=fragment.id,
            document_id=document.id,
            title=document.title,
            locator=fragment.locator,
            url=f"/api/sources/{fragment.id}",
        )

    def _claim(self, text: str, fragment: _Fragment, quote_text: str | None = None) -> Claim:
        return Claim(
            text=text,
            evidence=[Evidence(fragment_id=fragment.id, quote=quote_text or fragment.text)],
        )

    def ask(self, request: AskRequest) -> dict[str, Any]:
        question = request.question.lower()
        if "конфликт" in question or "противореч" in question:
            status = "conflict"
        elif "уточн" in question or "неоднознач" in question:
            status = "ambiguous"
        elif "недоступ" in question or "отказ" in question:
            status = "unavailable"
        elif "нет ответа" in question or "не найдено" in question or "отсутств" in question:
            status = "not_found"
        elif "частич" in question:
            status = "partial"
        else:
            status = "answered"

        document = self._latest_ready()
        fragment = self.fragments[document.fragment_id]
        source = self._source(fragment)
        warnings = [DEMO_WARNING]
        summary: list[Claim] = []
        steps: list[Claim] = []
        sources: list[Source] = []
        clarification: str | None = None
        verification: Verification | None = None
        answer_mode = "sources_only"

        if status in {"answered", "partial"}:
            summary = [
                self._claim(
                    "ДЕМО-ОТВЕТ: демонстрационный источник подтверждает порядок подачи заявки.",
                    fragment,
                )
            ]
            steps = [
                self._claim(
                    "ДЕМО-ШАГ 1: откройте демонстрационный источник и проверьте цитату.",
                    fragment,
                )
            ]
            sources = [source]
            answer_mode = "extracts"
            if status == "answered":
                verification = Verification(supported=True, complete=True, conflict=False, issues=[])
                warnings.append("ДЕМО: полный ответ имитируется; проверка модели не выполнялась.")
            else:
                verification = Verification(
                    supported=True,
                    complete=False,
                    conflict=False,
                    issues=["Полнота не подтверждена."],
                )
                warnings.append("ДЕМО: это частичный ответ, а не полная инструкция.")
        elif status == "conflict":
            first = self.fragments[BUILTIN_FRAGMENT_ID]
            second = self.fragments[BUILTIN_FRAGMENT_2_ID]
            summary = [
                self._claim("ДЕМО: один источник указывает на срок до начала события.", first),
                self._claim("ДЕМО: другой источник указывает на срок за два дня.", second),
            ]
            sources = [self._source(first), self._source(second)]
            answer_mode = "extracts"
            verification = Verification(
                supported=True,
                complete=False,
                conflict=True,
                issues=["Источники противоречат друг другу."],
            )
            warnings.append("ДЕМО: обнаружено противоречие; окончательный вывод не публикуется.")
        elif status == "ambiguous":
            clarification = "ДЕМО: уточните, о каком именно событии и подразделении идёт речь."
            warnings.append("ДЕМО: вопрос требует уточнения; поиск не выполнялся.")
        elif status == "not_found":
            warnings.append("ДЕМО: подходящих оснований в искусственном наборе нет.")
        else:  # unavailable
            warnings.append("ДЕМО: имитация недоступности модели и поиска; повторите позже вручную.")

        payload = {
            "status": status,
            "summary": [claim.model_dump(mode="json") for claim in summary],
            "steps": [claim.model_dump(mode="json") for claim in steps],
            "sources": [source.model_dump(mode="json") for source in sources],
            "warnings": warnings,
            "clarification": clarification,
            "mode": "bm25",
            "answer_mode": answer_mode,
            "verification": verification.model_dump(mode="json") if verification else None,
        }
        return AskResponse.model_validate(payload).model_dump(mode="json")

    # --------------------------------------------------------------- sources
    def source_page(self, fragment_id: str) -> str | None:
        fragment = self.fragments.get(fragment_id)
        if fragment is None:
            return None
        document = self.documents[fragment.document_id]
        title = html.escape(document.title)
        text = html.escape(fragment.text)
        locator = html.escape(_locator_label(fragment.locator))
        fragment_attr = html.escape(fragment.id, quote=True)
        return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>{title}</title>
  <link rel="stylesheet" href="/static/css/source.css">
</head>
<body>
  <main class="source-page">
    <p class="source-demo">ДЕМО-ПРЕДСТАВЛЕНИЕ ИСТОЧНИКА</p>
    <h1>{title}</h1>
    <p class="source-locator">{locator}</p>
    <article class="source-document" data-fragment-id="{fragment_attr}">
      <p id="fragment-{fragment_attr}"><mark>{text}</mark></p>
      <p class="source-note">Это искусственная выдержка заглушки, а не текст документа организации.</p>
    </article>
  </main>
</body>
</html>"""

    def original(self, document_id: str) -> tuple[_Document, bytes] | None:
        document = self.documents.get(document_id)
        if document is None or not document.content:
            return None
        return document, document.content

    # -------------------------------------------------------------- metadata
    def patch_metadata(self, document_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        document = self.documents.get(document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        try:
            patch = MetadataPatch.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc)) from exc

        metadata = dict(document.metadata)
        for field_name in patch.model_fields_set:
            value = getattr(patch, field_name)
            if field_name == "relations":
                metadata["relations"] = (
                    [relation.model_dump(mode="json") for relation in value]
                    if value is not None
                    else None
                )
            elif isinstance(value, date):
                metadata[field_name] = value.isoformat()
            else:
                metadata[field_name] = value

        valid_from = metadata.get("valid_from")
        valid_to = metadata.get("valid_to")
        if valid_from and valid_to and str(valid_from) > str(valid_to):
            raise HTTPException(status_code=422, detail="Начало действия позже окончания.")

        document.metadata = metadata
        if metadata.get("title"):
            document.title = _clean_title(str(metadata["title"]), document.title)
        # A real backend assigns ready only after publication. The synthetic
        # router deliberately does not fake that transition.
        return document.public()


def _json_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидается JSON-объект.")
    return payload


def build_ask_router(state: SyntheticState) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["synthetic"])

    @router.post("/ask")
    async def ask(request: Request) -> dict[str, Any]:
        try:
            payload = _json_body(await request.json())
            ask_request = AskRequest.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc)) from exc
        return state.ask(ask_request)

    return router


def build_documents_router(state: SyntheticState) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["synthetic"])

    @router.post("/documents")
    async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
        document, job, duplicate = await state.save_upload(file)
        return {
            "document_id": document.id,
            "job_id": job.id,
            "status": document.status,
            "duplicate": duplicate,
            "warnings": document.warnings,
            "demo": True,
        }

    @router.get("/documents")
    async def list_documents() -> dict[str, Any]:
        documents = sorted(state.documents.values(), key=lambda item: item.created, reverse=True)
        return {"documents": [document.public() for document in documents], "demo": True}

    @router.get("/jobs/{job_id}")
    async def job_status(job_id: str) -> dict[str, Any]:
        job = state.job_public(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Задание не найдено.")
        return job

    @router.post("/import-url")
    async def import_url(request: Request) -> dict[str, Any]:
        try:
            payload = _json_body(await request.json())
            import_request = ImportRequest.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc)) from exc
        url = state._check_url(import_request.url)
        if import_request.selected_urls:
            job = state.import_selected(url, import_request.selected_urls)
        else:
            job = state.import_preview(url)
        return {"job_id": job.id, "status": job.status, "demo": True}

    @router.patch("/documents/{document_id}/metadata")
    async def patch_metadata(document_id: str, request: Request) -> dict[str, Any]:
        payload = _json_body(await request.json())
        return state.patch_metadata(document_id, payload)

    @router.get("/sources/{fragment_id}", response_class=HTMLResponse)
    async def source(fragment_id: str) -> HTMLResponse:
        page = state.source_page(fragment_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Источник не найден.")
        return HTMLResponse(
            content=page,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
                    "base-uri 'none'; form-action 'none'"
                )
            },
        )

    @router.get("/documents/{document_id}/original")
    async def original(document_id: str) -> Response:
        result = state.original(document_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="Оригинал недоступен в демонстрационном режиме.",
            )
        document, content = result
        media_type = MEDIA_TYPES.get(document.media_type, "application/octet-stream")
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(document.name, safe='')}",
                "Cache-Control": "no-store",
            },
        )

    @router.get("/documents/{document_id}/review")
    async def get_review_demo(document_id: str) -> dict[str, Any]:
        doc = state.documents.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        frags = [f for f in state.fragments.values() if f.document_id == document_id]
        blocks = [
            {"index": i, "page": f.locator.get("page", 1), "text": f.text, "kind": "paragraph"}
            for i, f in enumerate(frags)
        ] or [{"index": 0, "page": 1, "text": f"Демонстрационное содержимое {doc.title}", "kind": "paragraph"}]
        return {
            "document": doc.public(),
            "blocks": blocks,
            "warnings": list(doc.warnings),
            "review_history": [],
            "original_info": {
                "media_type": doc.media_type,
                "page_count": 1,
                "pages": [{"page": 1, "width": 595.0, "height": 842.0, "rotation": 0}],
                "supports_bbox": doc.media_type == "pdf",
            },
        }

    @router.post("/documents/{document_id}/review")
    async def post_review_demo(document_id: str, request: Request) -> dict[str, Any]:
        doc = state.documents.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        payload = _json_body(await request.json())
        decision = payload.get("decision", "approved")
        if payload.get("action") == "reopen":
            return await get_review_demo(document_id)
        doc.metadata["reviewed"] = decision == "approved"
        if decision in {"needs_review", "rejected"}:
            doc.status = "needs_review"
        elif doc.status != "ready":
            doc.status = "ready"
        return await get_review_demo(document_id)

    @router.post("/documents/{document_id}/reocr")
    async def post_reocr_demo(document_id: str) -> dict[str, Any]:
        doc = state.documents.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        job = _Job(
            id=uuid4().hex,
            kind="process_document",
            created=time.monotonic(),
            delay=0.05,
            final_status="ready",
            payload={"document_id": document_id},
        )
        state.jobs[job.id] = job
        return {"document_id": document_id, "job_id": job.id, "status": "queued", "demo": True}

    @router.get("/documents/{document_id}/original-info")
    async def original_info_demo(document_id: str) -> dict[str, Any]:
        doc = state.documents.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        return {
            "document_id": doc.id,
            "title": doc.title,
            "name": doc.name,
            "download_url": f"/api/documents/{doc.id}/original",
            "media_type": doc.media_type,
            "page_count": 1,
            "pages": [{"page": 1, "width": 595.0, "height": 842.0, "rotation": 0}],
            "supports_bbox": doc.media_type == "pdf",
        }

    @router.get("/documents/{document_id}/original-page/{page_number}")
    async def original_page_demo(document_id: str, page_number: int) -> Response:
        doc = state.documents.get(document_id)
        if doc is None or doc.media_type != "pdf" or page_number != 1:
            raise HTTPException(status_code=404, detail="Страница недоступна.")
        from navigator.documents.highlighting import render_original_pdf_page
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_pdf = Path(tmpdir) / "demo.pdf"
            tmp_pdf.write_bytes(doc.content or b"")
            png = render_original_pdf_page(tmp_pdf, 1)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/sources/{fragment_id}/highlight")
    async def highlight_demo(fragment_id: str) -> dict[str, Any]:
        frag = state.fragments.get(fragment_id)
        if frag is None:
            raise HTTPException(status_code=404, detail="Источник не найден.")
        doc = state.documents[frag.document_id]
        return {
            "document_id": doc.id,
            "fragment_id": frag.id,
            "media_type": doc.media_type,
            "page": int(frag.locator.get("page") or 1),
            "page_count": 1,
            "page_width": 595.0,
            "page_height": 842.0,
            "exact": False,
            "boxes": [],
            "notice": "В демонстрационном режиме точные координаты фрагмента не вычисляются.",
            "title": doc.title,
            "download_url": f"/api/documents/{doc.id}/original",
        }

    return router
