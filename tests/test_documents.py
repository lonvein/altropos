"""Тесты подсистемы документов, хранилища, персистентной очереди и публикации."""
from datetime import date
from pathlib import Path
import pytest

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import DocumentService, DocumentStorage, JobQueue
from navigator.ingestion.common import ParseError
from navigator.schemas import MetadataPatch, Relation


@pytest.fixture
def store_and_service(tmp_path):
    root = tmp_path / "runtime"
    store = Store(root)
    settings = Settings(data_dir=root, profile="development")
    service = DocumentService(store=store, settings=settings)
    return store, service, root


def test_storage_safety_and_validation(tmp_path):
    storage = DocumentStorage(root=tmp_path, max_upload_mb=1)

    # Запрещенное расширение
    with pytest.raises(ParseError) as exc:
        storage.validate_file("malicious.exe", b"binary")
    assert "Неподдерживаемый формат" in str(exc.value)

    # Превышение размера файла
    big_content = b"x" * (2 * 1024 * 1024)
    with pytest.raises(ParseError) as exc:
        storage.validate_file("large.pdf", big_content)
    assert "Превышен максимальный размер" in str(exc.value)

    # Защита от выхода за пределы директории
    with pytest.raises(ValueError):
        storage._safe_path("../secret.txt")


def test_upload_deduplication(store_and_service):
    _, service, _ = store_and_service
    content = b"PDF-dummy-content-for-dedup"

    # Первая загрузка
    res1 = service.upload_file("test.pdf", content)
    assert res1["status"] == "queued"
    assert res1["duplicate"] is False
    doc_id = res1["document_id"]
    job_id = res1["job_id"]
    assert job_id is not None

    # Повторная загрузка того же содержимого
    res2 = service.upload_file("test_renamed.pdf", content)
    assert res2["document_id"] == doc_id
    assert res2["duplicate"] is True
    assert res2["job_id"] == job_id


def test_queue_lifecycle_and_recovery(store_and_service):
    store, service, _ = store_and_service
    queue = service.queue

    # Создаем 2 задания
    j1 = queue.enqueue("task1", {"data": 1})
    j2 = queue.enqueue("task2", {"data": 2})

    # Захватываем первое
    acquired1 = queue.acquire_next_job()
    assert acquired1["id"] == j1
    info1 = queue.get_job(j1)
    assert info1["status"] == "processing"

    # Имитация аварийного перезапуска сервиса
    recovered = queue.recover_stale_jobs()
    assert recovered == 1

    info1_after = queue.get_job(j1)
    assert info1_after["status"] == "queued"
    assert "перезапуска" in info1_after["error"]

    # После рестарта задача снова захватывается
    re_acquired = queue.acquire_next_job()
    assert re_acquired["id"] == j1
    queue.complete_job(j1, status="ready", result={"ok": True})
    assert queue.get_job(j1)["status"] == "ready"


def test_end_to_end_docx_processing_and_publishing(store_and_service, tmp_path):
    store, service, _ = store_and_service
    import docx

    # Генерируем реальный валидный DOCX в памяти
    d = docx.Document()
    d.add_paragraph("ПРИКАЗ от 01.09.2025 № 100-1")
    d.add_paragraph("Об утверждении регламента стажировок")
    d.add_paragraph("1. Общие положения")
    d.add_paragraph("1.1. Стажировка длится от одного до трёх месяцев.")
    docx_file = tmp_path / "order100.docx"
    d.save(docx_file)
    docx_bytes = docx_file.read_bytes()

    upload_res = service.upload_file("order100.docx", docx_bytes)
    doc_id = upload_res["document_id"]
    job_id = upload_res["job_id"]

    # Документ в статусе queued
    doc_before = service.get_document(doc_id)
    assert doc_before["status"] == "queued"

    # Выполняем задачу через воркер
    assert service.process_next_job() is True

    # Проверяем результат
    doc_after = service.get_document(doc_id)
    assert doc_after["status"] == "ready"
    assert "100-1" in doc_after["metadata"]["number"]
    assert doc_after["metadata"]["issued"] == "2025-09-01"

    job_info = service.queue.get_job(job_id)
    assert job_info["status"] == "ready"
    assert job_info["result"]["fragment_count"] >= 1

    # Проверяем, что фрагменты опубликованы в Store
    frags_in_db = store.rows("SELECT * FROM fragments WHERE document_id=?", (doc_id,))
    assert len(frags_in_db) >= 1
    assert any("Стажировка длится" in f["text"] for f in frags_in_db)

    # Проверяем, что кэш блоков сохранен для просмотрщика
    blocks = service.storage.get_extracted_blocks(doc_id)
    assert blocks is not None
    assert len(blocks) >= 3


def test_corrupted_file_isolation(store_and_service):
    store, service, _ = store_and_service

    # Загружаем поврежденный файл (битые байты с расширением .docx)
    corrupted_res = service.upload_file("broken.docx", b"not-a-valid-zip-file")
    bad_id = corrupted_res["document_id"]
    bad_job_id = corrupted_res["job_id"]

    # Загружаем следом корректный HTML файл
    valid_html = "<!DOCTYPE html><html><body><p>Корректный регламент</p></body></html>".encode("utf-8")
    good_res = service.upload_file("valid.html", valid_html)
    good_id = good_res["document_id"]
    good_job_id = good_res["job_id"]

    # Обрабатываем битый файл
    assert service.process_next_job() is True
    bad_doc = service.get_document(bad_id)
    assert bad_doc["status"] == "failed"
    bad_job = service.queue.get_job(bad_job_id)
    assert bad_job["status"] == "failed"
    assert "Ошибка обработки файла" in bad_job["error"]

    # Очередь НЕ остановилась: обрабатываем следующий корректный файл
    assert service.process_next_job() is True
    good_doc = service.get_document(good_id)
    assert good_doc["status"] == "ready"
    good_job = service.queue.get_job(good_job_id)
    assert good_job["status"] == "ready"


def test_patch_metadata(store_and_service, tmp_path):
    store, service, _ = store_and_service

    html_bytes = "<!DOCTYPE html><html><body><p>Порядок согласования</p></body></html>".encode("utf-8")
    upload = service.upload_file("rules.html", html_bytes)
    doc_id = upload["document_id"]
    service.process_next_job()

    # Обновляем метаданные через PATCH
    patch = MetadataPatch(
        title="Новый уточнённый заголовок",
        number="555-Р",
        issued=date(2025, 10, 15),
        valid_from=date(2025, 10, 15),
        valid_to=date(2026, 12, 31),
        reviewed=True,
        relations=[
            Relation(target_id="doc-old", kind="amends", confirmed=True)
        ]
    )
    updated = service.patch_metadata(doc_id, patch)
    assert updated["title"] == "Новый уточнённый заголовок"
    assert updated["metadata"]["number"] == "555-Р"
    assert updated["metadata"]["valid_to"] == "2026-12-31"
    assert updated["metadata"]["relations"][0]["kind"] == "amends"
    assert updated["metadata"]["relations"][0]["confirmed"] is True
