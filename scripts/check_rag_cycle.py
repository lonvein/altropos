"""Synthetic upload/ask/source probe against explicitly configured local models.

Usage: python scripts/check_rag_cycle.py [path/to/client.env]
Uses a new temporary DATA_DIR, never reads the real corpus, writes its result to
runtime/api-cycle-live.json. Requires running model endpoints; downloads no weights.
"""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values

for key, value in dotenv_values(sys.argv[1] if len(sys.argv) > 1 else ".env").items():
    if value is not None:
        os.environ[key] = value
root = Path(tempfile.mkdtemp(prefix="altropos-api-cycle-"))
os.environ["DATA_DIR"] = str(root)
os.environ["UI_SYNTHETIC_FALLBACK"] = "false"

from docx import Document
from fastapi.testclient import TestClient
from navigator.config import get_settings

get_settings.cache_clear()
from navigator.main import create_app

doc = Document()
doc.add_paragraph("ИСКУССТВЕННЫЙ УЧЕБНЫЙ РЕГЛАМЕНТ")
doc.add_paragraph("1. Подача заявки")
doc.add_paragraph("1.1. Для участия в учебном событии сотрудник направляет заявку координатору не позднее чем за три рабочих дня до начала события.")
doc.add_paragraph("1.2. Координатор подтверждает получение заявки в течение одного рабочего дня.")
content = io.BytesIO()
doc.save(content)
app = create_app(synthetic_fallback=False)
report = {"synthetic_document": True, "real_model_services": True}
with TestClient(app) as client:
    report["health"] = client.get("/health").json()
    started = time.monotonic()
    upload = client.post("/api/documents", files={"file": ("synthetic-cycle.docx", content.getvalue())})
    upload.raise_for_status()
    report["upload"] = upload.json()
    print(json.dumps({"upload": report["upload"]}, ensure_ascii=False), flush=True)
    for _ in range(360):
        job = client.get("/api/jobs/" + report["upload"]["job_id"]).json()
        if job["status"] in {"ready", "needs_review", "failed"}:
            break
        time.sleep(0.25)
    report["job"] = job
    report["index_seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps({"job": job, "seconds": report["index_seconds"]}, ensure_ascii=False), flush=True)
    assert job["status"] == "ready", job
    document_id = report["upload"]["document_id"]
    patched = client.patch(f"/api/documents/{document_id}/metadata", json={"reviewed": True, "valid_from": "2026-01-01"})
    patched.raise_for_status()
    assert patched.json()["status"] == "ready", patched.json()
    started = time.monotonic()
    response = client.post("/api/ask", json={"question": "Кому и за сколько дней направить заявку на учебное событие?", "as_of": "2026-11-13"})
    report["ask_http"] = response.status_code
    report["ask"] = response.json()
    report["ask_seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps({"ask": report["ask"], "seconds": report["ask_seconds"]}, ensure_ascii=False), flush=True)
    report["source_checks"] = []
    for source in report["ask"].get("sources", []):
        source_response = client.get(source["url"])
        quotes = [e["quote"] for claim in report["ask"]["summary"] + report["ask"]["steps"] for e in claim["evidence"] if e["fragment_id"] == source["fragment_id"]]
        assert source_response.status_code == 200
        assert all(q in source_response.text for q in quotes)
        report["source_checks"].append({"fragment_id": source["fragment_id"], "http": 200, "exact_quotes": True})
    assert client.get(f"/api/documents/{document_id}/original").status_code == 200
Path("runtime/api-cycle-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
assert report["health"]["application"]["data_mode"] == "real"
assert report["ask_http"] == 200
assert report["ask"]["answer_mode"] == "generated", report["ask"]
assert report["ask"]["verification"]["supported"] is True
print("PASS: upload -> ready/hybrid -> ask -> generated/verified -> source/original", flush=True)
