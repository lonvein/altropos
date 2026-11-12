"""Готовит пакет первых фрагментов для Клима и Глеба (этап 1).

Данные ВНУТРЕННИЕ: результат пишется в runtime/handoff/ (в .gitignore) и передаётся
через мессенджер, а не через GitHub. Использование:
    python scripts/make_handoff.py <папка с исходными файлами>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from navigator.ingestion import parse_file  # noqa: E402
from navigator.ingestion.common import document_ids, sha256_bytes, to_fragments  # noqa: E402
from navigator.schemas import Document  # noqa: E402

root = Path(sys.argv[1])
PICKS = {  # префикс имени файла -> как выбирать фрагменты
    "Регламент о командировании": ("pdf", 3, 1),
    "Приказ N 794-1": ("docx", 3, 1),
    "Копия Приложение 1б": ("xlsx", 2, 0),
}
documents, fragments = [], []
for prefix, (kind, with_section, tables) in PICKS.items():
    path = next(p for p in root.iterdir() if p.name.startswith(prefix))
    data = path.read_bytes()
    sha = sha256_bytes(data)
    doc_id, version = document_ids(sha)
    parsed = parse_file(path)
    frags = to_fragments(doc_id, version, parsed.build())
    chosen = []
    if kind == "xlsx":
        chosen = [f for f in frags if f.locator.kind == "xlsx" and "Колонки:" in f.text][:with_section]
    else:
        chosen = [f for f in frags if f.section][:with_section]
        chosen += [f for f in frags if " | " in f.text and f not in chosen][:tables]
    safe_name = f"{doc_id}.{kind}"
    doc = Document(id=doc_id, version=version, name=path.name, title=(parsed.title or path.stem)[:500],
                   sha256=sha, original_path=f"originals/{safe_name}", media_type=kind,
                   warnings=parsed.warnings)
    documents.append(doc.model_dump(mode="json"))
    fragments += [f.model_dump(mode="json") for f in chosen]

out = Path("runtime/handoff")
out.mkdir(parents=True, exist_ok=True)
payload = {"synthetic": False, "internal": True,
           "note": "ВНУТРЕННИЕ данные МФТИ. Не коммитить в GitHub, не передавать во внешние API.",
           "documents": documents, "fragments": fragments}
(out / "first_fragments.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"документов: {len(documents)}, фрагментов: {len(fragments)} -> {out / 'first_fragments.json'}")
for f in fragments:
    print(f["id"], f["section"] or "-", f["locator"])
