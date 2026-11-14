import math
import re
from functools import lru_cache
import pymorphy3

MORPH = pymorphy3.MorphAnalyzer()
SYNONYMS = {
    "флс": ["финансовый", "лицевой", "счет"],
    "цод": ["центр", "обработка", "данные"],
    "командировка": ["командирование", "поездка"],
    "общежитие": ["поселение", "проживание"],
    "эб": ["электронный", "бюджет"],
    "техподдержка": ["технический", "поддержка"],
}


@lru_cache(maxsize=40000)
def lemma(word):
    return MORPH.parse(word)[0].normal_form if word.isalpha() else word


def tokens(text):
    words = re.findall(r"[\w]+(?:[-.]\d+)*", text.lower().replace("ё", "е"))
    result = [lemma(w) for w in words]
    for w in list(result):
        result.extend(lemma(s) for s in SYNONYMS.get(w, []))
    return result or ["_"]


def estimate_tokens(text):
    # Deliberately conservative fallback. Actual LLM token counts are checked before inference.
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def normalize_quote(text):
    return re.sub(r"\s+", " ", text).strip()

