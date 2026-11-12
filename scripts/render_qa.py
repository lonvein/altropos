#!/usr/bin/env python3
"""Render the human-readable question set and a results template without extra deps."""
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "docs/qa/questions.json"
CORPUS = ROOT / "tests/fixtures/synthetic_corpus.json"
MARKDOWN = ROOT / "docs/qa/questions.md"
RESULTS = ROOT / "docs/results.csv"

CATEGORY_LABELS = {
    "single": "одиночный",
    "composite": "составной",
    "no_answer": "без ответа",
    "conflict": "конфликт",
    "ocr": "OCR",
    "table": "таблица",
    "revision": "редакция",
}


def escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(data, corpus):
    fragments = {fragment["id"]: fragment for fragment in corpus["fragments"]}
    documents = {document["id"]: document for document in corpus["documents"]}
    questions = data["questions"]
    lines = [
        "# Набор из 40 вопросов",
        "",
        "Сгенерировано из `docs/qa/questions.json`; не редактировать вручную.",
        "Все вопросы относятся к явно искусственному корпусу `tests/fixtures/synthetic_corpus.json`.",
        "",
        "## Состав",
        "",
        "| Владелец | Вопросов |",
        "|---|---:|",
    ]
    for owner, count in sorted(Counter(q["owner"] for q in questions).items()):
        lines.append(f"| {owner} | {count} |")
    lines += ["", "| Категория | Вопросов |", "|---|---:|"]
    for category, label in CATEGORY_LABELS.items():
        count = sum(q["category"] == category for q in questions)
        lines.append(f"| {label} | {count} |")

    for split, title in (("setup", "Настройка (20)"), ("final", "Финальная проверка (20)")):
        lines += ["", f"## {title}", ""]
        lines.append("| ID | Категория | Вопрос | as_of | Ожидаемый статус | Основания | Обязательные факты |")
        lines.append("|---|---|---|---|---|---|---|")
        for q in questions:
            if q["split"] != split:
                continue
            sources = []
            for fragment_id in q["expected_fragments"]:
                fragment = fragments[fragment_id]
                document = documents[fragment["document_id"]]
                sources.append(f"{document['title']} §{fragment['section']}")
            facts = "; ".join(q["required_facts"]) or "—"
            lines.append(
                "| {id} | {category} | {question} | {as_of} | {status} | {sources} | {facts} |".format(
                    id=q["id"],
                    category=CATEGORY_LABELS[q["category"]],
                    question=escape(q["question"]),
                    as_of=q["as_of"] or "—",
                    status=q["expected_status"],
                    sources=escape("; ".join(sources)) if sources else "—",
                    facts=escape(facts),
                )
            )
    lines.append("")
    MARKDOWN.write_text("\n".join(lines), encoding="utf-8")


def render_results_template(data):
    if RESULTS.exists():
        print(f"{RESULTS} exists; left unchanged")
        return
    questions = data["questions"]
    fields = [
        "question_id", "owner", "category", "split", "expected_status", "as_of",
        "a100_run", "a100_recall_at_k", "a100_citations_ok", "a100_latency_ms", "a100_notes",
        "gpu3060ti_run", "gpu3060ti_recall_at_k", "gpu3060ti_citations_ok",
        "gpu3060ti_latency_ms", "gpu3060ti_notes",
    ]
    with RESULTS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for q in questions:
            row = {
                "question_id": q["id"],
                "owner": q["owner"],
                "category": q["category"],
                "split": q["split"],
                "expected_status": q["expected_status"],
                "as_of": q["as_of"] or "",
            }
            for machine in ("a100", "gpu3060ti"):
                row[f"{machine}_run"] = "NOT_RUN"
                row[f"{machine}_recall_at_k"] = ""
                row[f"{machine}_citations_ok"] = ""
                row[f"{machine}_latency_ms"] = ""
                row[f"{machine}_notes"] = "Стенд не предоставлен; заглушки не использовались"
            writer.writerow(row)
    print(f"wrote {RESULTS}")


def main():
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    render_markdown(data, corpus)
    render_results_template(data)
    print(f"wrote {MARKDOWN}")


if __name__ == "__main__":
    main()
