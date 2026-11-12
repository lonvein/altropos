# Контракты v1

Источник истины — Pydantic-модели `navigator/schemas.py`. Неизвестные поля запрещены;
JSON Schema получается через `Model.model_json_schema()`. Протоколы вызовов находятся
в `navigator/interfaces.py`; это интерфейсы, а не готовые клиенты или HTTP handlers.
Все примеры ниже искусственные. Комментарий schema перед блоком JSON задаёт модель
для автоматической проверки в `tests/test_contracts.py`.

Минимальная проверка основы на Python 3.11: установить
`python -m pip install -r requirements-contracts.txt`, затем `python -m pytest -q`.
Она не требует существующих черновых requirements.txt/requirements-ml.txt,
парсеров, моделей и GPU. Эти черновики и navigator/text.py не входят в PR основы.

## Документы и происхождение

`Document` — неизменяемая версия файла: id/version, SHA-256 байтов оригинала,
относительный путь внутри DATA_DIR, формат, статус и метаданные. Новое содержимое
получает новые ID версии/документа/фрагментов. Повтор SHA возвращает прежний ID.
`MetadataPatch` содержит извлечённые даты, номер и подтверждаемые связи `Relation`;
при PATCH различать отсутствующее поле и явный null через model_fields_set.
Проверять даты после наложения patch на всю запись. Подтверждение review и переходы
очереди реализует Егор, ready назначает только публикация. Сам reviewed не заменяет
контроль needs_review. Источник и актуальность при неизвестных датах остаются неизвестными.

`Fragment` содержит document_id/document_version, стабильный id, ordinal от нуля,
исходный text без изменения пробелов, section и discriminated union locator:

| kind | Обязательные поля | Значение |
|---|---|---|
| pdf | page ≥ 1 | bbox опционален: [x0,y0,x1,y1], пункты от левого верхнего угла |
| docx | block ≥ 0 | Индекс извлечённого блока, включая таблицы |
| xlsx | sheet, range | Имя листа, A1 или A1:B2; доступные значения не вычисляются |
| html | block ≥ 0 | Индекс блока сохранённого снимка |

Парсер обеспечивает соответствие range настоящим ячейкам и bbox размеру страницы.
Фрагменты одного документа имеют уникальные ID и последовательные ordinal от нуля.
Locator.kind совпадает с Document.media_type. Перестановка пробелов в цитатах запрещена
для публикуемого evidence: quote должна быть точной подстрокой Fragment.text.

## Публикация

Будущий `Indexer.index_document(document_id, fragments) -> IndexResult` принимает
зарегистрированный документ и полный набор фрагментов, строит индекс, вызывает Store.publish.
Это синхронная операция для фонового worker, не для event loop API.
Ошибка даёт ComponentError(ErrorDetail), не успешный IndexResult. Повтор идентичного
набора идемпотентен: индексатор повторно использует publication_id или проверяет активный digest.

`Store.register_document(Document) -> str` возвращает канонический ID, который
нужно использовать в фрагментах (в том числе при дедупликации).
`Store.publish(IndexManifest, list[Fragment], warnings) -> IndexResult` принимает
уже записанные файлы, проверяет хеши и атомарно активирует публикацию.
Хранилище не строит BM25/FAISS. Низкоуровневые ошибки Store (ValueError, OSError,
sqlite3.Error) будущий адаптер преобразует в ComponentError; пути и traceback клиенту не выдавать.
Для получения строк ready служит active_publications(fingerprint), а не прямой SELECT fragments.

Манифест содержит publication_id, документ/версию, mode, digest фрагментов,
артефакт BM25 и для hybrid — vectors плюс embedding_fingerprint.
Артефакт = относительный путь в indexes/<publication_id>/ и SHA-256.
В BM25 поля vectors/fingerprint равны null, предупреждение обязательно в результате.
Fingerprint = `<model>@<revision>:1024:normalized`. Совпадение размерности без совпадения
model/revision не разрешает загружать векторный индекс.
Digest = SHA-256 UTF-8 JSON списка model_dump(mode='json'), отсортированного по ordinal;
sort_keys=True, ensure_ascii=False, separators=(',', ':'). Реализация — fragments_digest.
Старые публикации остаются неизменными. Файлы не удалять при активных читателях.

## Поиск и клиенты

| Вызов (async, кроме Indexer) | Вход → выход | Обязательные проверки адаптера |
|---|---|---|
| Searcher.search | SearchRequest → SearchResult | ready-снимок, as_of, лимит профиля |
| EmbeddingsClient.embed | EmbeddingRequest → EmbeddingResponse | Столько векторов, сколько texts, в исходном порядке; fingerprint совпадает |
| RerankerClient.rerank | RerankRequest → RerankResponse | Каждый входной ID ровно один раз; ID уникальны; фиксированная ревизия |
| LLMClient.generate | GenerationRequest → Draft | Контекст/выход по профилю; корректный JSON |
| LLMClient.verify | VerificationRequest → Verification | Отдельный последовательный вызов; поддержка, полнота, конфликты |

EmbeddingResponse: конечные числа, 1024 координаты, L2-норма с допуском 0.01 по
квадрату нормы. Batch от 1 до 64. Reranker принимает от 1 до 40 фрагментов.
Оценки score не являются вероятностями; score_kind явно различает BM25/RRF/reranker.
SearchHit: фрагмент, rank с единицы, score и score_kind. SearchResult: до 8 уникальных
hits в порядке rank, режим, закреплённые publication_ids и предупреждения.
SearchRequest.limit и итоговый набор дополнительно ограничиваются top_fragments профиля.
Двухсторонние ограничения (вход против выхода клиента) проверяет адаптер, отдельная
Pydantic-модель ответа не может сама узнать исходный запрос.

Клиенты возбуждают ComponentError с кодами model_unavailable, model_timeout,
invalid_model_output, context_exceeded; ретраи ограничены orchestration-слоем.
Подзапросы: Subqueries максимум 3; исходный вопрос всегда сохраняется.

## Утверждения, проверка и HTTP

Claim = text и непустой evidence[]; Evidence = fragment_id и дословная quote.
Draft не содержит придуманных titles/URL. Сначала проверять JSON, IDs, подстроки и
соответствие текущему поисковому снимку, затем смысл/условия/исключения и полноту.
Verification.supported означает, что весь публикуемый набор прошёл нужные проверки;
complete — полноту процедуры в доступных материалах; conflict — обнаруженный конфликт.
Pydantic проверяет структуру, но не доказывает смысл цитат. Для extracts достаточно
программно подтверждённых дословных выдержек, без семантического обобщения.

`POST /api/ask`: AskRequest(question, as_of?). Отсутствующий as_of фиксируется сервером
как текущая дата приложения один раз на запрос; тесты используют явную дату.
Успешный обработанный запрос возвращает HTTP 200 и AskResponse. Source строится
сервером из закреплённых фрагментов/метаданных; url строго `/api/sources/{fragment_id}`.
Timings — миллисекунды по монотонным часам; неисполненная стадия = 0.

| status | Смысл |
|---|---|
| answered | Есть утверждения, supported=true, complete=true, conflict=false |
| partial | Есть проверенные утверждения; полнота не обещается, есть предупреждение |
| not_found | Нет достаточного основания; утверждения пусты |
| ambiguous | Нужны уточнения; clarification содержит вопрос по необходимости |
| conflict | Подтверждённые основания противоречат друг другу; нельзя объявлять итог окончательным |
| unavailable | Невозможно дать результат; утверждения пусты, причина в warnings |

mode = hybrid/bm25. answer_mode = generated/extracts/sources_only.
Без LLM источники возвращаются как partial/ambiguous только при наличии проверенных
выдержек, иначе not_found с источниками и объяснением отсутствия генерации;
unavailable — если даже поиск недоступен. sources_only не содержит claims.
При неудачной проверке после одного исправления — extracts с точными выдержками
и partial, либо sources_only без выводов. Все ограниченные режимы имеют warnings.
Лимиты и ручное переключение A100/3060 Ti/4B описаны в architecture.md и Settings.

Ошибки HTTP (ErrorResponse) отделены от содержательных статусов AskResponse:

| HTTP | code |
|---|---|
| 422 | invalid_request (включая обёртку FastAPI RequestValidationError) |
| 404 | not_found (для отсутствующего источника/документа) |
| 409 | not_ready, index_mismatch |
| 502 | invalid_model_output при прямом вызове сервиса без возможного fallback |
| 503 | model_unavailable, busy |
| 504 | model_timeout |
| 500 | storage_error, index_publish_failed, internal_error |
| 422 | context_exceeded, когда вопрос нельзя поместить даже без источников |

Флаги retryable=true допустимы для временных отказов. Преобразование ошибок и
FastAPI handlers ещё не реализованы. Если доступен содержательный fallback,
ask-router отвечает 200 с ограничением вместо ошибки транспорта.

## Валидируемые примеры

Часть примеров опускает поля со значениями по умолчанию; тесты валидируют каждый
блок и сверяют evidence с текстами искусственного fixture. Наличие служебных
искусственных файлов индекса в тестах проверяет атомарность, а не корректность поиска.

<!-- schema: Document -->
```json
{
  "id": "demo-d1-v1",
  "version": "v1",
  "name": "demo-1.pdf",
  "title": "Искусственный учебный документ 1",
  "sha256": "bc72862b03360805bf10c90c64043fa5e7b4556f043a57359cabb422de8fe720",
  "original_path": "originals/demo-1.pdf",
  "media_type": "pdf",
  "status": "queued",
  "source_url": null,
  "retrieved_at": null,
  "metadata": {
    "title": null,
    "number": null,
    "issued": null,
    "valid_from": null,
    "valid_to": null,
    "reviewed": null,
    "relations": null
  },
  "warnings": []
}
```

<!-- schema: Fragment -->
```json
{
  "id": "demo-f1-v1",
  "document_id": "demo-d1-v1",
  "document_version": "v1",
  "ordinal": 0,
  "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
  "section": "1",
  "locator": {
    "kind": "pdf",
    "page": 2,
    "bbox": [
      0.0,
      0.0,
      100.0,
      100.0
    ]
  }
}
```

<!-- schema: Fragment -->
```json
{
  "id": "demo-f2-v1",
  "document_id": "demo-d2-v1",
  "document_version": "v1",
  "ordinal": 0,
  "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Координатор отвечает в течение двух учебных дней.",
  "section": "1",
  "locator": {
    "kind": "docx",
    "block": 0
  }
}
```

<!-- schema: Fragment -->
```json
{
  "id": "demo-f3-v1",
  "document_id": "demo-d3-v1",
  "document_version": "v1",
  "ordinal": 0,
  "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Категория: демонстрационная. Лимит: 10 условных единиц.",
  "section": "1",
  "locator": {
    "kind": "xlsx",
    "sheet": "Демо",
    "range": "A1:B2"
  }
}
```

<!-- schema: Fragment -->
```json
{
  "id": "demo-f4-v1",
  "document_id": "demo-d4-v1",
  "document_version": "v1",
  "ordinal": 0,
  "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Для учебного события требуется подтверждение координатора.",
  "section": "1",
  "locator": {
    "kind": "html",
    "block": 0
  }
}
```

<!-- schema: AskRequest -->
```json
{
  "question": "Как направить учебную заявку?",
  "as_of": "2026-11-12"
}
```

<!-- schema: IndexManifest -->
```json
{
  "publication_id": "demo-p1",
  "document_id": "demo-d1-v1",
  "document_version": "v1",
  "fragments_sha256": "9bf80ef24714eb0ca7fa5ead081999a18703136cf9a66edef2eac33abca772fb",
  "mode": "bm25",
  "bm25": {
    "path": "indexes/demo-p1/bm25.json",
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  }
}
```

<!-- schema: IndexResult -->
```json
{
  "document_id": "demo-d1-v1",
  "publication_id": "demo-p1",
  "mode": "bm25",
  "fragment_count": 1,
  "warnings": [
    "Векторы отсутствуют."
  ]
}
```

<!-- schema: SearchResult -->
```json
{
  "mode": "bm25",
  "hits": [
    {
      "fragment": {
        "id": "demo-f1-v1",
        "document_id": "demo-d1-v1",
        "document_version": "v1",
        "ordinal": 0,
        "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
        "section": "1",
        "locator": {
          "kind": "pdf",
          "page": 2,
          "bbox": [
            0.0,
            0.0,
            100.0,
            100.0
          ]
        }
      },
      "rank": 1,
      "score": 1.5,
      "score_kind": "bm25"
    }
  ],
  "publication_ids": [
    "demo-p1"
  ],
  "warnings": [
    "Работает только BM25."
  ]
}
```

<!-- schema: EmbeddingRequest -->
```json
{
  "texts": [
    "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события."
  ],
  "purpose": "document"
}
```

<!-- schema: RerankRequest -->
```json
{
  "question": "Как направить заявку?",
  "fragments": [
    {
      "id": "demo-f1-v1",
      "document_id": "demo-d1-v1",
      "document_version": "v1",
      "ordinal": 0,
      "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
      "section": "1",
      "locator": {
        "kind": "pdf",
        "page": 2,
        "bbox": [
          0.0,
          0.0,
          100.0,
          100.0
        ]
      }
    }
  ]
}
```

<!-- schema: RerankResponse -->
```json
{
  "model": "synthetic-reranker",
  "revision": "demo-v1",
  "scores": [
    {
      "fragment_id": "demo-f1-v1",
      "score": 0.5
    }
  ]
}
```

<!-- schema: GenerationRequest -->
```json
{
  "question": "Как направить заявку?",
  "as_of": "2026-11-12",
  "fragments": [
    {
      "id": "demo-f1-v1",
      "document_id": "demo-d1-v1",
      "document_version": "v1",
      "ordinal": 0,
      "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
      "section": "1",
      "locator": {
        "kind": "pdf",
        "page": 2,
        "bbox": [
          0.0,
          0.0,
          100.0,
          100.0
        ]
      }
    }
  ]
}
```

<!-- schema: Draft -->
```json
{
  "status": "partial",
  "summary": [
    {
      "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
      "evidence": [
        {
          "fragment_id": "demo-f1-v1",
          "quote": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события."
        }
      ]
    }
  ],
  "warnings": [
    "Искусственный пример; полнота не проверялась."
  ]
}
```

<!-- schema: VerificationRequest -->
```json
{
  "question": "Как направить заявку?",
  "fragments": [
    {
      "id": "demo-f1-v1",
      "document_id": "demo-d1-v1",
      "document_version": "v1",
      "ordinal": 0,
      "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
      "section": "1",
      "locator": {
        "kind": "pdf",
        "page": 2,
        "bbox": [
          0.0,
          0.0,
          100.0,
          100.0
        ]
      }
    }
  ],
  "draft": {
    "status": "partial",
    "summary": [
      {
        "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
        "evidence": [
          {
            "fragment_id": "demo-f1-v1",
            "quote": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события."
          }
        ]
      }
    ],
    "warnings": [
      "Искусственный пример; полнота не проверялась."
    ]
  }
}
```

<!-- schema: Verification -->
```json
{
  "supported": true,
  "complete": false,
  "issues": [
    "Полнота не подтверждена."
  ]
}
```

<!-- schema: AskResponse -->
```json
{
  "status": "partial",
  "summary": [
    {
      "text": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события.",
      "evidence": [
        {
          "fragment_id": "demo-f1-v1",
          "quote": "ИСКУССТВЕННЫЙ ПРИМЕР. Заявка направляется координатору до начала учебного события."
        }
      ]
    }
  ],
  "warnings": [
    "Искусственный пример; полнота не проверялась."
  ],
  "sources": [
    {
      "fragment_id": "demo-f1-v1",
      "document_id": "demo-d1-v1",
      "title": "Искусственный учебный документ 1",
      "locator": {
        "kind": "pdf",
        "page": 2,
        "bbox": [
          0.0,
          0.0,
          100.0,
          100.0
        ]
      },
      "url": "/api/sources/demo-f1-v1"
    }
  ],
  "mode": "bm25",
  "answer_mode": "extracts",
  "verification": {
    "supported": true,
    "complete": false,
    "issues": [
      "Полнота не подтверждена."
    ]
  }
}
```

<!-- schema: AskResponse -->
```json
{
  "status": "not_found",
  "mode": "bm25",
  "answer_mode": "sources_only",
  "warnings": [
    "LLM отключена; подходящих источников нет."
  ]
}
```

<!-- schema: ErrorResponse -->
```json
{
  "error": {
    "code": "model_timeout",
    "message": "Локальная модель не ответила вовремя.",
    "retryable": true
  }
}
```
