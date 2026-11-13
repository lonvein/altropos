"""Пакетный аудит локальной директории документов: парсинг, OCR, метаданные и фрагменты."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from navigator.ingestion import SUPPORTED, parse_file  # noqa: E402
from navigator.ingestion.common import ParseError, document_ids, sha256_bytes, to_fragments  # noqa: E402
from navigator.ingestion.ocr import make_ocr  # noqa: E402


def audit_directory(
    source_dir: Path,
    *,
    ocr_enabled: bool = True,
) -> dict[str, Any]:
    """Проверяет все поддерживаемые документы в source_dir и возвращает сводный отчёт."""
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Директория не найдена: {source_dir}")

    files = sorted(
        p for p in source_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    ocr_fn = make_ocr() if ocr_enabled else None

    results: list[dict[str, Any]] = []
    counts = {"ready_candidate": 0, "needs_review": 0, "error": 0}
    total_started = time.perf_counter()

    for path in files:
        started = time.perf_counter()
        ext = path.suffix.lower()
        media_type = SUPPORTED[ext]
        item: dict[str, Any] = {
            "filename": path.name,
            "media_type": media_type,
            "size_bytes": path.stat().st_size,
        }
        try:
            content = path.read_bytes()
            sha = sha256_bytes(content)
            doc_id, version = document_ids(sha)
            parsed = parse_file(path, ocr=ocr_fn) if media_type == "pdf" else parse_file(path)
            drafts = parsed.build()
            fragments = to_fragments(doc_id, version, drafts)

            status = "needs_review" if (parsed.needs_review or not fragments) else "ready_candidate"
            counts[status] += 1
            meta = parsed.metadata
            item.update({
                "document_id": doc_id,
                "status": status,
                "unit_count": len(parsed.units),
                "block_count": len(parsed.blocks),
                "fragment_count": len(fragments),
                "warnings": list(parsed.warnings),
                "metadata": meta.model_dump(mode="json") if meta else {},
                "elapsed_sec": round(time.perf_counter() - started, 3),
            })
        except ParseError as exc:
            counts["error"] += 1
            item.update({
                "status": "error",
                "error": str(exc),
                "elapsed_sec": round(time.perf_counter() - started, 3),
            })
        except Exception as exc:  # noqa: BLE001 - аудит не должен падать на одном файле
            counts["error"] += 1
            item.update({
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_sec": round(time.perf_counter() - started, 3),
            })
        results.append(item)

    return {
        "source_dir": str(source_dir),
        "total_files": len(files),
        "counts": counts,
        "total_elapsed_sec": round(time.perf_counter() - total_started, 3),
        "files": results,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    """Формирует Markdown-сводку аудита без сохранения полного текста документов."""
    lines = [
        "# Аудит локального корпуса документов",
        "",
        f"- Всего файлов: **{report['total_files']}**",
        f"- Готовы к публикации (`ready_candidate`): **{report['counts']['ready_candidate']}**",
        f"- Требуют проверки (`needs_review`): **{report['counts']['needs_review']}**",
        f"- Ошибки (`error`): **{report['counts']['error']}**",
        f"- Общее время: **{report['total_elapsed_sec']} с**",
        "",
        "| Файл | Тип | Статус | Блоки | Фрагменты | Номер | Связи | Время (с) | Предупреждения |",
        "|---|---|---|---:|---:|---|---:|---:|---|",
    ]
    for item in report["files"]:
        meta = item.get("metadata") or {}
        number = meta.get("number") or "—"
        rels = len(meta.get("relations") or [])
        warns = "; ".join(item.get("warnings") or []) or item.get("error") or "—"
        warns = warns.replace("|", "/")
        lines.append(
            f"| {item['filename']} | {item['media_type']} | {item['status']} | "
            f"{item.get('block_count', 0)} | {item.get('fragment_count', 0)} | "
            f"{number} | {rels} | {item['elapsed_sec']} | {warns} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Аудит парсинга и метаданных локальной папки документов.")
    parser.add_argument("source_dir", type=Path, help="Путь к папке с документами")
    parser.add_argument(
        "--out-json",
        type=Path,
        default=ROOT / "runtime" / "handoff" / "corpus_audit.json",
        help="Путь для JSON-отчёта",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        default=ROOT / "runtime" / "handoff" / "corpus_audit.md",
        help="Путь для Markdown-отчёта",
    )
    parser.add_argument("--no-ocr", action="store_true", help="Отключить OCR при аудите PDF")
    args = parser.parse_args()

    report = audit_directory(args.source_dir, ocr_enabled=not args.no_ocr)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(render_markdown_report(report), encoding="utf-8")

    print(
        f"Audited {report['total_files']} files in {report['total_elapsed_sec']}s: "
        f"{report['counts']}"
    )
    return 0 if report["counts"]["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
