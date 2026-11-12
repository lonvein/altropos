import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = json.loads((ROOT / "docs/qa/questions.json").read_text(encoding="utf-8"))
CORPUS = json.loads((ROOT / "tests/fixtures/synthetic_corpus.json").read_text(encoding="utf-8"))

ALLOWED_STATUSES = {"answered", "partial", "not_found", "ambiguous", "conflict", "unavailable"}
CATEGORIES = {"single", "composite", "no_answer", "conflict", "ocr", "table", "revision"}
EXPECTED_SPLIT_COUNTS = {
    "single": 6,
    "composite": 4,
    "no_answer": 3,
    "conflict": 1,
    "ocr": 2,
    "table": 2,
    "revision": 2,
}


def documents():
    return {document["id"]: document for document in CORPUS["documents"]}


def fragments():
    return {fragment["id"]: fragment for fragment in CORPUS["fragments"]}


def test_declared_split_matches_questions():
    assert QUESTIONS["synthetic"] is True
    assert len(QUESTIONS["questions"]) == 40
    assert QUESTIONS["split"] == {"setup": 20, "final": 20}
    for split in QUESTIONS["split"]:
        assert sum(q["split"] == split for q in QUESTIONS["questions"]) == 20


def test_owner_and_category_counts():
    owners = Counter(q["owner"] for q in QUESTIONS["questions"])
    assert owners == {"Глеб": 28, "Егор": 12}

    categories = Counter(q["category"] for q in QUESTIONS["questions"])
    assert categories == {
        "single": 12,
        "composite": 8,
        "no_answer": 6,
        "conflict": 2,
        "ocr": 4,
        "table": 4,
        "revision": 4,
    }


def test_each_split_keeps_categories():
    for split in ("setup", "final"):
        counts = Counter(q["category"] for q in QUESTIONS["questions"] if q["split"] == split)
        assert counts == EXPECTED_SPLIT_COUNTS


def test_expected_status_distribution():
    statuses = Counter(q["expected_status"] for q in QUESTIONS["questions"])
    assert statuses == {
        "answered": 30,
        "partial": 1,
        "not_found": 6,
        "ambiguous": 1,
        "conflict": 2,
    }


def test_question_ids_are_unique_and_stable():
    ids = [q["id"] for q in QUESTIONS["questions"]]
    assert len(ids) == len(set(ids))
    for question_id in ids:
        assert re.fullmatch(r"Q-[GE]-[A-Z]+-\d{2}", question_id), question_id


def test_each_question_is_complete_and_grounded():
    docs = documents()
    frags = fragments()
    for q in QUESTIONS["questions"]:
        label = q["id"]
        assert q["owner"] in {"Глеб", "Егор"}, label
        assert q["category"] in CATEGORIES, label
        assert q["split"] in {"setup", "final"}, label
        assert q["expected_status"] in ALLOWED_STATUSES, label
        assert isinstance(q["title"], str) and q["title"].strip(), label
        assert isinstance(q["question"], str) and len(q["question"]) >= 10, label
        assert isinstance(q["required_facts"], list), label
        assert isinstance(q["forbidden_claims"], list), label
        assert isinstance(q["expected_fragments"], list), label
        assert isinstance(q["expected_documents"], list), label
        assert len(q["expected_fragments"]) == len(set(q["expected_fragments"])), label
        assert len(q["expected_documents"]) == len(set(q["expected_documents"])), label
        if q["as_of"] is not None:
            assert date.fromisoformat(q["as_of"]), label

        for fragment_id in q["expected_fragments"]:
            assert fragment_id in frags, f"{label}: unknown fragment {fragment_id}"
        actual_documents = {frags[fragment_id]["document_id"] for fragment_id in q["expected_fragments"]}
        assert actual_documents == set(q["expected_documents"]), label
        assert actual_documents <= set(docs), label

        texts = [frags[fragment_id]["text"] for fragment_id in q["expected_fragments"]]
        for fact in q["required_facts"]:
            assert any(fact in text for text in texts), f"{label}: fact not grounded: {fact!r}"

        if q["expected_status"] in {"answered", "partial"}:
            assert q["expected_fragments"], f"{label}: answer needs sources"
            assert q["expected_documents"], f"{label}: answer needs documents"
            assert q["required_facts"], f"{label}: answer needs required facts"
        if q["expected_status"] == "not_found":
            assert not q["expected_fragments"], label
            assert not q["expected_documents"], label
            assert not q["required_facts"], label
        if q["expected_status"] == "ambiguous":
            assert not q["expected_fragments"], label
            assert not q["expected_documents"], label
            assert not q["required_facts"], label
            assert q["clarification"], f"{label}: clarification is required"
        if q["expected_status"] == "conflict":
            assert len(q["expected_documents"]) >= 2, f"{label}: conflict needs two documents"
            assert len(q["expected_fragments"]) >= 2, f"{label}: conflict needs two fragments"
            assert q["required_facts"], label


def test_conflict_questions_are_explicit():
    conflicts = [q for q in QUESTIONS["questions"] if q["expected_status"] == "conflict"]
    assert len(conflicts) == 2
    for question in conflicts:
        assert question["owner"] == "Глеб"
        assert question["category"] == "conflict"
        assert question["forbidden_claims"], question["id"]


def test_no_answer_questions_do_not_require_retrieval():
    no_answer = [q for q in QUESTIONS["questions"] if q["category"] == "no_answer"]
    assert len(no_answer) == 6
    for question in no_answer:
        assert question["expected_status"] == "not_found"
        assert question["expected_fragments"] == []
        assert question["expected_documents"] == []


def test_egor_questions_cover_his_categories():
    egor = [q for q in QUESTIONS["questions"] if q["owner"] == "Егор"]
    assert Counter(q["category"] for q in egor) == {"ocr": 4, "table": 4, "revision": 4}


def test_corpus_is_explicitly_synthetic():
    assert CORPUS["synthetic"] is True
    assert "ИСКУССТВЕННЫЙ" in CORPUS["notice"]
    for document in CORPUS["documents"]:
        assert document["synthetic"] is True
        assert document["title"].startswith("ИСКУССТВЕННЫЙ ПРИМЕР"), document["id"]
    for fragment in CORPUS["fragments"]:
        assert fragment["text"].startswith("ИСКУССТВЕННЫЙ ПРИМЕР"), fragment["id"]


def test_corpus_structure_and_locators():
    docs = documents()
    frags = CORPUS["fragments"]
    assert len({fragment["id"] for fragment in frags}) == len(frags)
    by_document = {}
    for fragment in frags:
        assert fragment["document_id"] in docs, fragment["id"]
        assert fragment["locator"]["kind"] == docs[fragment["document_id"]]["media_type"], fragment["id"]
        by_document.setdefault(fragment["document_id"], []).append(fragment["ordinal"])
    for document_id, ordinals in by_document.items():
        assert sorted(ordinals) == list(range(len(ordinals))), document_id


def test_no_internal_or_secret_material():
    serialized = json.dumps(CORPUS, ensure_ascii=False) + json.dumps(QUESTIONS, ensure_ascii=False)
    forbidden = ["Данные/", "Основная информация", "/home/", "mipt.ru", "password", "api_key", "secret"]
    for marker in forbidden:
        assert marker not in serialized, marker
