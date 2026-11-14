# Отчёт аудита altropos — 13.11.2026

## Проверяемый объект и окружение

| Параметр | Значение |
|---|---|
| Объект | Архив `altropos-main.zip`, переданный пользователем 13.11.2026 |
| Git | `.git` в архиве отсутствует → ветку, SHA и состояние дерева установить нельзя. `docs/audit/README.md` ссылается на код `2038de0…`; соответствие архива этому SHA **не подтверждено** |
| Дата / часовой пояс | 2026-11-13, локальное время машины проверки |
| ОС | macOS (Darwin 24.6.0), arm64 |
| Python | 3.11.18 в отдельном venv; зависимости по `requirements-dev.txt` (закреплённые версии, faiss-cpu 1.11.0) |
| OCR | Tesseract 5.5.2 (локально, для искусственных сканов) |
| Docker | Демон недоступен: сборка и запуск образов не выполнялись; `docker compose config` — статически |
| Модели / GPU / стенд / VPS | Не использовались по условию задания. Зависимые проверки — NOT_VERIFIED |
| Проверяющий | Claude (модель Opus 5.5): координатор и 5 агентов-аудиторов по зонам |
| Оригинал ТЗ (DOCX) | Недоступен — полнота реестра относительно оригинала не сверялась; проверены только внутренняя согласованность реестра (52 ID, суммы 100/30) |

Исходный код в ходе аудита **не изменялся**. Единственное добавление в архив — этот каталог `docs/audit/reports/2026-11-13-nogit/`. Все временные данные, искусственные fixtures и скрипты сценариев хранились вне репозитория и в отчёт включены только как описания.

## Краткий результат

По 52 ID реестра: **PASS 10, PARTIAL 36, FAIL 3, NOT_VERIFIED 2, BLOCKED 1** (полностью — в [compliance-matrix.md](compliance-matrix.md)). Официальные баллы не присваиваются; статусы — покрытие доказательствами в границах искусственных данных без моделей.

FAIL:
- **TZ-T1.1 / PRJ-03** — на искусственных сканах с номерами пунктов в отдельной колонке (висячий отступ) OCR отрывает номера «1.1. 1.2. …» от текста, искажает часть номеров, а документ получает `ready` (F-ING-1, F-ING-2). Это тот же эффект, что на присланном скриншоте фрагмента «Регламент». Автонумерация Word в DOCX теряется полностью (F-ING-3).
- **TZ-T6.1** — запуск по README `start.sh 4b --with-models` не проходит: профильная загрузка моделей не скачивает BGE (F-DEPLOY-1); `smoke.sh` не импортирует `navigator` в контейнере (F-DEPLOY-2); compose не передаёт REVIEW_DIR/CORPUS_MANIFEST (F-DEPLOY-4).

## Подтверждённые функции (искусственные данные)

- Приём PDF/DOCX/XLSX, SHA-дедупликация, новые версии, повреждённые файлы → `failed` без остановки очереди; пустой скан → `needs_review`.
- Определение поворота 0/90/180/270° и обратное преобразование координат; различение «|» и «1» без глобальной замены; пропуски/заполнители таблиц не выдумываются; формулы без cached value объясняются; таблицы не размножаются в просмотре.
- `needs_review` не индексируется; изменённый пакет блокирует публикацию; сбой публикации сохраняет прежний ready; OCR-одобрение не ставит `metadata.reviewed`; поздняя дата не означает отмену.
- Цитаты проверяются по ID и дословной подстроке; одно исправление; `sources_only` без claims; общий deadline и занятость модели; название и URL добавляет сервер.
- Экранирование содержимого, CSP sandbox на странице источника, нет CDN/телеметрии, адрес модели не попадает в браузер, административные маршруты закрыты в gateway (403).
- Compose-конфигурации валидны (4 профиля, 2 GPU-override); архитектура и use cases описаны; UC-2 (загрузка → ready → sources_only → рестарт) воспроизведён без моделей.

## Существенные ошибки и пробелы

| Серьёзность | Находки |
|---|---|
| high | F-ING-1 (отрыв номеров пунктов в OCR, скриншот 1), F-ING-3 (потеря автонумерации DOCX), F-CS-1 (BM25 без морфологии: 28/33 ожидаемых фрагментов в top-8 на 40 вопросах проекта, 2/6 перефразировок), F-DEPLOY-1 |
| medium | F-ING-2, F-ING-4, F-CS-2, F-CS-3, F-RAG-API-1 (технический отказ модели показывается как «Информация не найдена»), F-RAG-API-3, F-UI-1 (сырой JSON локатора на странице источника, скриншот 1), F-UI-2 (публичный оригинал `needs_review`), F-DEPLOY-2…5 |
| low / риск | остальные, см. [findings.md](findings.md) |

Не проверено: качество ответов реальной LLM (смысл, полнота процедур, конфликты, инъекции), hybrid/embeddings/reranker, набор качества 20/20 (runner отсутствует, `results.csv` — 40×NOT_RUN), Docker-сборка, VPS/HTTPS/Tailscale, GPU-профили, выступление команды.

## Наблюдения со скриншотов пользователя

1. `/api/sources/<id>` — над текстом выводится `{"kind": "pdf", "page": 1, "bbox": [...]}`: это `json.dumps(locator)` в `navigator/documents/router.py:121-125`, закреплённый тестом `tests/test_document_router.py:57` (F-UI-1). Текст фрагмента из одних номеров «1.1. … 2.2.» воспроизведён на искусственном скане (F-ING-1); на текстовом PDF с той же разметкой разбор корректен.
2. Карточка «Страница 2: Числовой токен уточнён согласованным OCR…» — штатное предупреждение о перечитывании номера (`ocr.py`); при этом номера в отдельной колонке не перечитываются (F-ING-2).

## Команды

Полный прогон, выполненный координатором:

| Команда | Каталог | Код | Итог | Длительность |
|---|---|---|---|---|
| `python -m pytest -q -p no:cacheprovider` | корень репозитория | 1 | 466 passed, 1 failed (`test_model_clients.py::test_connection_refused` — зависит от ОС, см. F-RAG-API-2), 2 skipped (Windows-only) | 24 с |
| `docker info` | — | timeout | демон недоступен | — |

Команды аудиторов зон:

### Обработка и OCR

| # | Команда | Каталог | Код выхода | Итог | Длительность |
|---|---|---|---|---|---|
| 1 | `DATA_DIR=<workdir>/work/ingestion/data <workdir>/venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_ingestion.py tests/test_ocr_layout.py tests/test_ocr_numbering.py tests/test_ocr_runtime.py tests/test_docx_completeness.py tests/test_table_gaps.py` | корень репозитория | 0 | 101 passed, 1 warning (Starlette deprecation) | 3.5 с |
| 2 | `PYTHONPATH=<repo> <workdir>/venv311/bin/python <workdir>/work/ingestion/scripts/run_pdf.py <workdir>/work/ingestion/fixtures/pdf` → `out/run_pdf.txt` | `<workdir>/work/ingestion` | 0 | native ×2, scan 0/90/180/270, scan_wide, mixed (с OCR и без), blank, truncated/garbage/zero; воспроизведён F-ING-1/2 | 10 с |
| 3 | `… run_pdf2.py <workdir>/work/ingestion/fixtures/pdf2` → `out/run_pdf2.txt` | `<workdir>/work/ingestion` | 0 | зазоры 92/100pt, Times 300 dpi, вертикальные черты/номера 3.1/2.0/2.9; F-ING-5, PRJ-04 | ~8 с |
| 4 | `… run_office.py <workdir>/work/ingestion/fixtures/office` → `out/run_office.txt` | `<workdir>/work/ingestion` | 0 | автонумерация, символы шрифтов, таблица с объединениями, смета с формулами, усечённые DOCX/XLSX; F-ING-3/4 | ~2 с |
| 5 | inline `prepare_document` для table.docx и smeta.xlsx (out: `out/review/`) | `<workdir>/work/ingestion` | 0 | видимый main 1×, fragments 1×, html 2× (второй — свёрнутый раздел фрагментов) | ~1 с |
| 6 | `… run_service.py <workdir>/work/ingestion` (DATA: `data_svc/`) → `out/run_service.txt` | `<workdir>/work/ingestion` | 0 | статусы ready/needs_review/failed, публикация scan_rot0 и autonum.docx; F-ING-1/3/7 | ~5 с |
| 7 | inline: notes.pdf / notes_scan.pdf через `parse_pdf` | `<workdir>/work/ingestion/scripts` | 0 | F-ING-8 | ~2 с |
| 8 | inline: pua.pdf (PUA в нативном PDF) | `<workdir>/work/ingestion/scripts` | 0 | генератор не встраивает PUA-глифы (`(cid:0)`), результат не использован как доказательство PRJ-08 для PDF | <1 с |

Скрипты: `<workdir>/work/ingestion/scripts/{gen.py,run_pdf.py,run_pdf2.py,run_office.py,run_service.py}`; выводы: `<workdir>/work/ingestion/out/`.

### Корпус, публикация, поиск

| Команда | Каталог | Код выхода | Итог | Длительность |
|---|---|---|---|---|
| `<workdir>/venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_corpus.py tests/test_review.py tests/test_retrieval.py tests/test_documents.py tests/test_question_set.py tests/test_ocr_runtime.py` (PYTHONDONTWRITEBYTECODE=1) | репозиторий | 0 | 96 passed, 5 warnings (лог `<workdir>/work/corpus-search/pytest-zone.log`) | 1,3 с |
| `python exp_store.py <workdir>/work/corpus-search/data-store` (PYTHONPATH=репозиторий) | <workdir>/work/corpus-search | 0 | S1, S3a, S3b, S3c, S5 — поведение описано в матрице (флаг `ok=false` у S5 вызван ошибкой ожидания в скрипте аудитора: при as_of до valid_from новый документ корректно скрыт) | 0,3 с |
| `python exp_review.py <workdir>/work/corpus-search/data-review` | <workdir>/work/corpus-search | 0 | S0, S2, S2b, S4, S6 | 0,3 с |
| `python eval_bm25.py <repo> data-eval-reviewed reviewed` и `… unreviewed` | <workdir>/work/corpus-search | 0 / 0 | recall@8 28/33, @4 25/33 (одинаково в обоих режимах метаданных) | ~1 с |
| `python scale.py data-scale 50 200` | <workdir>/work/corpus-search | 0 | индекс 1,6 с, запрос 1,9–2,4 с | ~6 с |
| `python -c "extract_metadata(...)"` | <workdir>/work/corpus-search | 0 | valid_from = issued | <1 с |

Что выполняет набор 40 вопросов: проектного runner нет. tests/test_question_set.py проверяет только состав, разбиение 20/20, категории, существование фрагментов и вхождение фактов в тексты искусственного корпуса. scripts/render_qa.py строит таблицу. scripts/check_rag_cycle.py — один синтетический цикл upload/ask с живыми моделями, вопросы набора он не использует. В docs/results.csv все 40 строк `NOT_RUN`; docs/results.md это честно фиксирует. Измерение BM25 выше сделано скриптом аудитора и не является оценкой качества RAG.

Примечание: каталоги `navigator/*/__pycache__` в репозитории появились в 16:23, до моих запусков с `PYTHONDONTWRITEBYTECODE=1`; вероятно, их создали параллельные проверки. Я их не трогал.

### Ответ, проверка, API

| Команда | Каталог | Код выхода | Итог | Длительность |
|---|---|---|---|---|
| `<workdir>/venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_answer_validation.py tests/test_ask_router.py tests/test_pipeline.py tests/test_model_clients.py` (DATA_DIR=<workdir>/work/rag-api/data) | корень репозитория | 1 | 145 passed, 1 failed (test_connection_refused) | 7,0 с |
| `... pytest -q -p no:cacheprovider tests/test_model_clients.py::test_connection_refused` ×3 | корень репозитория | 1 (pytest: «1 failed» каждый раз; код выхода в zsh-цикле не сохранён) | AssertionError: model_timeout ≠ model_unavailable | ≈5,5 с каждый |
| `python -I conn_probe.py` | <workdir>/work/rag-api | 0 | bound-not-listening → TimeoutError 5,0 с; освобождённый порт → ConnectionRefusedError 0,000 с | ≈10 с |
| `python -I -c <connect probe>` без песочницы; `sysctl net.inet.tcp.blackhole` | — | 0 | TimeoutError 2,0 с; blackhole=0 | ≈2 с |
| `python -I refused_transport.py <repo>` | <workdir>/work/rag-api | 0 | освобождённый порт → model_unavailable retryable 0,01 с; bound → model_timeout | ≈1 с |
| `python -I scenarios.py <repo> <workdir>/work/rag-api` (лог scenarios.log) | <workdir>/work/rag-api | 0 | 27 сценариев. «Не выполнены» по замыслу S3b, S5b, S6b (риск F-3) и S8×3 (F-1); остальные соответствуют контракту | 4,3 с |
| `python -I e2e_main.py <repo> <workdir>/work/rag-api` | <workdir>/work/rag-api | 0 | create_app: ask real, 200 bm25 sources_only, source 200 с экранированием, 422 ×2 | ≈1 с |

### UI и безопасность

| Команда | Каталог | Код выхода | Итог | Длительность |
|---|---|---|---|---|
| `env -u UI_SYNTHETIC_FALLBACK <workdir>/venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_ui.py tests/test_document_router.py tests/test_health.py tests/test_deployment.py` (лог `<workdir>/work/ui/pytest_zone.log`) | репозиторий | 0 | 39 passed, 1 warning (Starlette deprecation) | 10 s |
| `PYTHONDONTWRITEBYTECODE=1 <workdir>/venv311/bin/python -m pytest -q -p no:cacheprovider tests/test_deploy_laptop.py` (лог `<workdir>/work/ui/pytest_laptop.log`) | репозиторий | 0 | 22 passed, 2 skipped (Windows-only) | 1 s |
| `PYTHONDONTWRITEBYTECODE=1 <workdir>/venv311/bin/python scenario.py <repo> <workdir>/work/ui` (DEBUG, лог `<workdir>/work/ui/scenario.log`) | `<workdir>/work/ui` | 0 | 48/51 true; несоответствия: нет ссылки на оригинал, `<script>` в JSON (не уязвимость), текст PDF в DEBUG-журнале | 1 s |
| `AUDIT_LOG_LEVEL=INFO … scenario.py` (лог `<workdir>/work/ui/scenario_info.log`) | `<workdir>/work/ui` | 0 | 49/51 true; в INFO-журнале текста документов нет | 1 s |
| `… needs_review_original.py <repo> <workdir>/work/ui` | `<workdir>/work/ui` | 0 | оригинал `needs_review` через gateway → 200 (F-UI-2) | 1 s |
| `uvicorn scripts.deploy.gateway:create_app --factory --app-dir <repo> --host 127.0.0.1 --port 18765` (DATA_DIR=`<workdir>/work/ui/uvdata`, PUBLIC_ORIGIN=`https://reader.test`) + `curl` | `<workdir>/work/ui` | 0 (остановлен kill) | `/`=200 с CSP, `/documents` `/review` `/api/documents` `/openapi.json`=403, `/health` санитизирован, ask → `not_found` на пустом корпусе | ~3 s |
| `grep -rn` по `navigator services config` (sentry/telemetry/analytics/CDN, http(s)-URL, offline-флаги, innerHTML) | репозиторий | 0/1 | внешних ресурсов и телеметрии нет; `innerHTML` в `static/js` нет | <1 s |

Сценарии: `<workdir>/work/ui/scenario.py`, `<workdir>/work/ui/needs_review_original.py`; артефакты: `<workdir>/work/ui/source_pdf.html`, `<workdir>/work/ui/source_html_0.html`, `<workdir>/work/ui/ask_*.json`, `<workdir>/work/ui/app_logs.txt` (последний прогон INFO).

### Развёртывание, документация, демо

| Команда | Каталог | Код выхода | Итог | Длительность |
|---|---|---:|---|---|
| `grep -oE 'TZ-…\|PRJ-…' requirements.md \| sort -u`, awk-суммы весов | исходный репозиторий (только чтение) | 0 | 52 ID; 100/30; группы согласованы | <1 с |
| Проверка локальных ссылок (bash/grep) в docs/audit, README, docs, demo, qa | исходный репозиторий | 0 | битых ссылок нет | <1 с |
| `cp -R` исходного дерева → `<workdir>/work/deploy/repo` и `<workdir>/work/deploy/repo-git` | <workdir>/work/deploy | 0 | изолированные копии | <5 с |
| `docker compose …` через плагин с подменённым DOCKER_CONFIG | <workdir>/work/deploy/repo | 125 | плагин compose не найден (из-за подмены конфига); заменено на standalone | <1 с |
| `docker-compose --env-file .env --env-file config/profiles/{development,4b,3060ti,a100}.env -f docker-compose.yml config --quiet` | <workdir>/work/deploy/repo (`.env` = копия `.env.example`) | 0 ×4 | валидно | ~1 с |
| то же с `-f compose.a100.yml` / `-f compose.3060ti.yml`; без `.env` | <workdir>/work/deploy/repo | 0 ×3 | валидно; 3060ti: CUDA-образ llama.cpp, `driver: nvidia`, app на 127.0.0.1:8000 | ~1 с |
| `bash -n scripts/*.sh` | <workdir>/work/deploy/repo | 0 | синтаксис корректен; 9 скриптов исполняемые | <1 с |
| `bash scripts/check_hygiene.sh` (без .git) | <workdir>/work/deploy/repo | 0 | **ложный PASS**: 4× «not a git repository» → «passed» (F-DEPLOY-3) | 0,06 с |
| `git init && git add -A && commit`; `bash scripts/check_hygiene.sh` | <workdir>/work/deploy/repo-git (временный Git) | 0 | 151 файл; все 4 проверки OK | 0,06 с |
| Ручной скан: типы файлов, файлы >200 КБ, личные пути, IP, e-mail, токены | исходный репозиторий | 0 | находок нет (только regex в check_hygiene.sh и docs/security.md) | <1 с |
| `DATA_DIR=<workdir>/work/deploy/data python -m pytest -q -p no:cacheprovider tests/test_deployment.py tests/test_deploy_laptop.py tests/test_health.py tests/test_question_set.py` | <workdir>/work/deploy/repo | 0 | 47 passed, 2 skipped (только Windows), 1 warning | 1,8 с |
| `python -I download_probe.py <repo> <tmp>` (заглушка huggingface_hub, без сети) | <workdir>/work/deploy/probe | 0 | 4b/3060ti/a100 → только GGUF; ml/all → BGE (F-DEPLOY-1) | <1 с |
| Симуляция `test -f` из start.sh:70 на фиктивной раскладке volume | <workdir>/work/deploy/probe/fake_models | 1 / 0 | только GGUF → 1; GGUF+BGE → 0 | <1 с |
| `python scripts/smoke.py` | <workdir>/work/deploy/repo | 1 | `ModuleNotFoundError: navigator` (F-DEPLOY-2); сетевых вызовов не было | <1 с |
| `python envcmp.py` (Settings ↔ .env.example ↔ compose) | <workdir>/work/deploy/repo | 0 | 4 поля Settings не передаются в контейнер app (F-DEPLOY-4) | <1 с |
| `python -I uc2_probe.py <repo> <workdir>/work/deploy/uc2/data`; `uc2_src.py` | <workdir>/work/deploy/probe | 0 | аналог UC-2 без моделей пройден; `/api/import-url` 404 в режиме real | 0,7 с |

Полный прогон тестов (466 passed, 1 failed, 2 skipped) взят из контекста брифа и в этой зоне не повторялся. Логи и пробы лежат в `<workdir>/work/deploy/` (hygiene-nogit.log, hygiene-git.log, compose-3060ti.yaml, probe/*.py).

#### Use case на искусственных данных

| UC | Вход | Ожидаемо (use-cases.md) | Фактически в этом прогоне |
|---|---|---|---|
| UC-2, шаг 1 | Вопрос «Сколько стоит парковка…» к пустой базе, LLM и ML выключены | `not_found` или `sources_only`, без выдуманной цены | 200: `status=not_found`, `mode=bm25`, `answer_mode=sources_only`, 0 источников, 0 claims, 2 предупреждения. Совпадает. |
| UC-2, шаг 2 | Загрузка искусственного `demo-parking.docx` (python-docx, в <workdir>/work), затем повторная загрузка | `queued → ready`; повтор возвращает тот же ID | upload 200 `queued`, job `ready`, повтор: `same_id=True`. Совпадает. |
| UC-2, шаг 3 | Повторный вопрос | `answered`, «100 условных единиц» (с LLM) | Без LLM: `sources_only`, 1 источник «demo-parking», `/api/sources/…-f0000` 200 и содержит «100 условных», предупреждение «LLM отключена». Ветка `answered` NOT_VERIFIED: нет моделей. |
| UC-2, шаг 4 | Новый экземпляр приложения на том же DATA_DIR | Документ и источник сохраняются | Тот же источник найден. Совпадает (перезапуск процесса, не Docker). |
| Некорректный ввод | `{"question":" "}` | Явная ошибка | 422 |
| UC-1 | Вопрос о мероприятии на 35 участников, профиль a100 | `answered`, 2 факта, 2 источника | NOT_VERIFIED: требуется профиль A100 и LLM, GPU запрещён. Корпус `syn-org-amend-v1` есть в tests/fixtures/synthetic_corpus.json. |

## Разделение доказательств

- **Новые результаты этого прогона:** всё выше, на искусственных данных, без моделей.
- **Исторические сведения (не перепроверялись):** «469 passed» на `2038de0`, «15 ready / 749 фрагментов», «22,27 с», зелёный CI, состояние DNS/VPS — из `docs/audit/*`.
- **Гипотезы:** влияние F-ING-1/F-ING-3 на реальный корпус (в архиве его нет) — вероятно по скриншоту, но число затронутых документов не установлено.

## Файлы отчёта

- [compliance-matrix.md](compliance-matrix.md) — 52 ID, статусы и доказательства
- [findings.md](findings.md) — 39 находок по зонам
- [next-actions.md](next-actions.md) — сводный приоритет и действия по зонам
