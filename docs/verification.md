# Проверка развёртывания и границы подтверждённого

Дата проверки: **2026-11-12**. Ветка: `feature/web-deploy`, коммит `fe9cc2a`
(плюс незакоммиченные файлы развёртывания, перечисленные в PR).

Документ фиксирует только то, что действительно выполнялось в этом окружении.
Всё остальное явно помечено как непроверенное. Запуск на A100 и 3060 Ti, реальные
ответы моделей и работа Docker-стека здесь **не подтверждены**.

## 1. Окружение проверки

| Параметр | Значение |
|---|---|
| ОС | Arch Linux, ядро `7.2.9-arch1-1`, x86_64 |
| Python | 3.14.7 (локальный `.venv`), не штатная версия CI |
| Зависимости | более новые, чем пины: `fastapi 0.162.5`, `pydantic 2.14.3` |
| Docker CLI | `docker compose` 5.6.0 |
| Docker daemon | **недоступен**: `permission denied ... unix:///var/run/docker.sock` |
| GPU | **отсутствует**: `nvidia-smi` не найден |
| Память / диск | 13 GiB RAM, 236 GiB свободно на `/home` |
| Веса моделей | отсутствуют |
| `torch`, `sentence-transformers`, `llama-server` | не установлены |

Пользователь не входит в группу `docker`, `sudo` без пароля недоступен. Поэтому
сборка образов, `docker compose up`, health-проверки контейнеров и проверка
volume выполнены быть не могли.

## 2. Что проверено

### 2.1. Тесты Python

```bash
.venv/bin/python -m pytest -q
# 143 passed, 1 warning in 6.28s
```

Единственное предупреждение — устаревание `httpx` в `starlette.testclient`; оно не
относится к логике приложения. Тесты не требуют Docker, GPU и весов.

Покрыто тестами этого дерева:

- три сервиса Compose, отсутствие `ports:` у `llm` и `embeddings-reranker`;
- согласованность `config/profiles/*.env` с `navigator/config.py` и
  `config/models.lock.json`;
- отсутствие секретов и `hf_`-значений в `.env.example`;
- отсутствие весов и всего контекста в образе приложения;
- исполняемость скриптов запуска, остановки, backup/restore;
- один снимок `runtime` (SQLite + оригиналы + извлечённый текст + индексы);
- UI, `/health`, синтетический корпус и разметка 40 вопросов.

### 2.2. Синтаксис скриптов и чистота diff

```bash
for f in scripts/*.sh; do bash -n "$f"; done   # ошибок нет
git diff --check                               # exit 0
```

### 2.3. Валидация Compose без запуска

```bash
docker compose --env-file .env.example \
  --env-file config/profiles/<profile>.env -f docker-compose.yml config --quiet
```

Успешно (exit 0) для `development`, `4b`, `3060ti`, `a100`, а также для
`compose.a100.yml` и `compose.3060ti.yml`. Проверена интерполяция переменных и
синтаксис, **не** запуск контейнеров.

### 2.4. Ревизии и контрольные суммы весов

Метаданные Hugging Face проверены запросом к API для каждой закреплённой ревизии:

```bash
# для каждого repo/revision из config/models.lock.json
GET https://huggingface.co/api/models/<repo>/revision/<revision>?blobs=true
```

| Репозиторий | Ревизия | Результат |
|---|---|---|
| `Qwen/Qwen2.5-14B-Instruct-GGUF` | `b466e1f8...` | 200, три шарда, SHA-256 совпадают |
| `Qwen/Qwen3-8B-GGUF` | `7c41481f...` | 200, SHA-256 совпадает |
| `Qwen/Qwen3-4B-GGUF` | `bc640142...` | 200, SHA-256 совпадает |
| `BAAI/bge-m3` | `5617a9f6...` | 200, `pytorch_model.bin`, SHA-256 совпадает |
| `BAAI/bge-reranker-v2-m3` | `953dc6f6...` | 200, `model.safetensors`, SHA-256 совпадает |

Все 7 файлов из `config/models.lock.json` совпали с `lfs.sha256` в метаданных
Hugging Face. Это подтверждает **корректность lock-файла**, но не скачивание:
сами веса на диск не загружались, локальные SHA-256 не пересчитывались.

### 2.5. Локальный запуск приложения без Docker

```bash
PROFILE=development LLM_ENABLED=false ML_ENABLED=false \
DATA_DIR=/home/linuxoid/tmp/opencode/altropos-smoke \
UI_SYNTHETIC_FALLBACK=true \
.venv/bin/python -m uvicorn navigator.main:app --host 127.0.0.1 --port 8010
```

Фактические результаты:

| Запрос | Результат |
|---|---|
| `GET /health` | 200; `status: limited`; `profile: development`; ML и LLM `disabled` с причинами |
| `GET /` | 200, HTML «Задать вопрос · Altropos» |
| `GET /documents` | 200, HTML-страница документов |
| `GET /static/css/app.css` | 200, `text/css` |
| Заголовки | `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, CSP |
| Остановка процесса | штатная, без зависших процессов |

Это подтверждает, что приложение собирается, отдаёт UI и корректно сообщает об
ограниченном режиме. Ответы получены от **явно помеченной синтетической заглушки**,
а не от RAG-модели. Роутеры `navigator.rag`/`navigator.documents` в этом дереве не
подключены.

## 3. Что не проверено

| Пункт | Причина | Как проверить |
|---|---|---|
| Сборка `Dockerfile` и `Dockerfile.ml` | Docker daemon недоступен | `docker compose build` на машине с Docker |
| Запуск трёх сервисов, их healthcheck | Docker daemon недоступен | `./scripts/start.sh 4b --with-models` |
| Сохранность `runtime` после `stop`/`restart` | Docker недоступен | сценарий из `docs/deployment.md`, раздел 6 |
| `backup.sh` / `restore.sh` на реальном volume | Docker недоступен | снимок → restore → `PRAGMA integrity_check` |
| Скачивание весов и SHA-256 | веса не скачивались | `./scripts/download-models.sh 4b` |
| `/embed` и `/rerank` на BGE | нет `torch`, весов и запущенного ML-сервиса | `./scripts/smoke.sh` |
| `/v1/chat/completions` Qwen | нет `llama-server` и GGUF | `./scripts/smoke.sh` |
| Профиль A100: 14B, 16K контекст, VRAM | нет GPU и A100 | прогон на A100, запись в `docs/results.md` |
| Профиль 3060 Ti: 8B в 8 ГБ VRAM | нет GPU; влезание не измерено | прогон на 3060 Ti; при OOM — ручной переход на `4b` |
| Профиль `4b` на CPU: скорость и RAM | не запускался | прогон на целевой машине |
| SSH-туннель | нет удалённого хоста | `./scripts/tunnel.sh <user>@<host>` |
| GitHub Actions CI | workflow не запускался в этом окружении | push/PR в GitHub |
| OCR `tesseract rus+eng` и `poppler` в образе | образ не собирался | загрузка PDF/DOCX после сборки |
| Штатный Python 3.12 и закреплённый `pydantic 2.11.4` | локально Python 3.14 | CI-джоб `tests` |

## 4. Условия, при которых результат можно считать подтверждённым

1. Сборка и запуск выполнены на машине с доступным Docker daemon.
2. `/health` возвращает `status: ok` для выбранного профиля, а не `limited`.
3. `./scripts/smoke.sh` получает непустые ответы `/embed`, `/rerank` и
   `/v1/chat/completions` без fallback.
4. Для A100/3060 Ti записаны фактическая VRAM, время загрузки и ответ на
   контрольный вопрос; при нехватке памяти выполнен ручной переход на `4b`.
5. Снимок `backup.sh` восстановлен на чистом volume, `integrity_check` — `ok`,
   контрольный вопрос повторён.

До выполнения этих пунктов фразы «работает на A100», «помещается в 8 ГБ» и
«модель отвечает» считаются неподтверждёнными.
