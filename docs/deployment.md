# Развёртывание Altropos

Документ описывает воспроизводимый запуск из чистой копии репозитория. Фактические
результаты проверок и список непроверенного — в [verification.md](verification.md).

## 1. Состав и адреса

| Сервис Compose | Роль | Внутренний адрес | Публикация на хосте |
|---|---|---|---|
| `app` | FastAPI, UI, `/health`, SQLite | `app:8000`, изнутри `127.0.0.1:8000` | только `127.0.0.1:${APP_PORT}` |
| `embeddings-reranker` | BGE-M3 + BGE reranker v2 m3 | `http://embeddings-reranker:8081` | нет |
| `llm` | llama.cpp server, OpenAI-compatible | `http://llm:8080/v1` | нет |

Имена и порты — предложенный контракт с Климом. При изменении нужно одновременно
поправить `docker-compose.yml`, `navigator/config.py` (defaults), `.env.example` и
этот документ. В контейнере `127.0.0.1` не указывает на соседний сервис: приложение
получает `ML_URL` и `LLM_URL` из Compose.

Модельные сервисы не имеют `ports:` и доступны только в сети Compose. Приложение
по умолчанию слушает `127.0.0.1`; удалённый доступ — через SSH-туннель.

## 2. Требования

- Linux x86_64, Docker Engine 24+, Docker Compose v2 (проверено на Compose 5.6).
- Для A100/3060 Ti: NVIDIA driver, `nvidia-container-toolkit`, runtime `nvidia`.
- Диск: ~7 ГиБ для профиля 4B (LLM + BGE), ~10 ГиБ для 3060 Ti, ~16 ГиБ для A100.
  Оценки по размерам файлов; фактический запас VRAM не измерялся.
- RAM: BGE-M3 и reranker на CPU занимают около 4.5 ГиБ весов в fp32 плюс запас
  Python/torch. Для 4B рекомендуется 16 ГиБ RAM; это ориентир, не гарантия.
- `curl` для проверок; `git` для фиксации ревизии снимка.
- Системные пакеты OCR/PDF уже есть в образе: `tesseract-ocr`, `tesseract-ocr-rus`,
  `tesseract-ocr-eng`, `poppler-utils`.

## 3. Профили

| Параметр | `a100` | `3060ti` | `4b` | `development` |
|---|---|---|---|---|
| LLM | Qwen2.5-14B-Instruct Q4_K_M | Qwen3-8B Q4_K_M | Qwen3-4B Q4_K_M | нет |
| Контекст | 16384 | 4096 | 4096 | 4096 |
| Бюджет материалов | 8000 | 2200 | 2200 | 2200 |
| Максимум ответа | 1536 | 768 | 768 | 768 |
| Фрагментов | 8 | 4 | 4 | 4 |
| BGE | GPU | CPU | CPU | нет |
| Файл профиля | `config/profiles/a100.env` | `config/profiles/3060ti.env` | `config/profiles/4b.env` | `config/profiles/development.env` |

Профиль задаётся только конфигурацией: `scripts/start.sh <profile>` передаёт
`--env-file config/profiles/<profile>.env`. Код приложения не меняется. Значения
контекста и бюджета проверяются `navigator/config.py`; Qwen3 запускается с
`--reasoning-budget 0`, клиент дополнительно передаёт `enable_thinking=false`.
`--parallel 1` обеспечивает один запрос к LLM одновременно.

Переключение между A100, 3060 Ti и 4B — только вручную. Автоматического fallback нет.

## 4. Получение весов

Веса не входят в Git и в образ. Они скачиваются отдельной командой в постоянный
volume `altropos_models`; сервисы монтируют его read-only в `/models`.

```bash
# Резервный профиль: Qwen3-4B + BGE-M3 + reranker (~7 GiB)
./scripts/download-models.sh 4b

# Основной профиль: Qwen2.5-14B (три шарда) + BGE (~12-13 GiB)
./scripts/download-models.sh a100

# Все LLM-профили и BGE (~20 GiB)
./scripts/download-models.sh all
```

Скрипт использует закреплённые ревизии и SHA-256 из
[`config/models.lock.json`](../config/models.lock.json), проверяет контрольные суммы
и не сохраняет токены. При необходимости токен передаётся только через окружение:

```bash
export HF_TOKEN=...   # не записывать в .env и не коммитить
./scripts/download-models.sh 4b
unset HF_TOKEN
```

Скачивание выполняется во временном контейнере `python:3.12-slim-bookworm` с
`huggingface_hub==2.4.0`; на хост не устанавливаются ML-зависимости.

Если веса нужны для другого каталога/volume, задайте `MODELS_VOLUME` и
`PYTHON_IMAGE`. Не заменяйте файлы в volume вручную без обновления lock-файла.

## 5. Запуск и остановка

```bash
cp .env.example .env
./scripts/doctor.sh 4b          # проверка Docker, диска, RAM и GPU
./scripts/download-models.sh 4b
./scripts/start.sh 4b --with-models
```

`start.sh` собирает образ приложения, проверяет наличие весов, поднимает три сервиса,
ждёт `/health` и печатает адрес, команду туннеля и команды проверки. Если весов нет,
он всё равно поднимает приложение, но не запускает модельные сервисы и явно пишет
об ограниченном режиме. `--with-models` превращает отсутствие весов в ошибку.

```bash
./scripts/smoke.sh              # health + реальные /embed, /rerank, /v1/chat/completions
./scripts/stop.sh               # остановка, volume сохраняются
./scripts/stop.sh --purge --yes # удалить ТОЛЬКО runtime volume
```

`stop.sh` не удаляет volume весов. Для полного удаления весов:
`docker volume rm altropos_models`.

### A100 и SSH-туннель

На A100 приложение слушает `127.0.0.1:8000`. С рабочего места:

```bash
./scripts/tunnel.sh <user>@<a100-host>
# или: ssh -N -L 8000:127.0.0.1:8000 <user>@<a100-host>
```

Открыть `http://127.0.0.1:8000`. Не менять `APP_BIND` на `0.0.0.0` и не публиковать
`8080/8081` наружу; для доступа используйте туннель или доверенную LAN с firewall.

## 6. Данные и постоянное хранилище

`runtime` — именованный Docker volume `altropos_runtime`, смонтированный в
`/data/runtime`. Внутри: `navigator.sqlite`, `originals/`, `extracted/`, `indexes/`.
`DATA_DIR` в контейнере всегда `/data/runtime`; значение из `.env` применяется только
при локальном запуске вне Compose.

Данные переживают `stop.sh` и `docker compose down`, потому что volume не удаляется.
Для проверки:

```bash
./scripts/start.sh development
docker compose exec -T app sh -c 'mkdir -p /data/runtime && echo survived > /data/runtime/persist-check'
./scripts/stop.sh
./scripts/start.sh development
docker compose exec -T app cat /data/runtime/persist-check
```

## 7. Резервное копирование и восстановление

Снимок — один `.tar.gz` со всей `DATA_DIR`: SQLite после `wal_checkpoint(TRUNCATE)`,
оригиналы, извлечённый текст и индексы. Живая SQLite-база не копируется.

```bash
./scripts/backup.sh --label before-update
# backups/altropos-runtime-<UTC>-before-update.tar.gz
# + .sha256 + .json с профилем и git revision

./scripts/restore.sh backups/altropos-runtime-...tar.gz --yes
```

`backup.sh` останавливает только `app`, делает checkpoint, архивирует volume от root
(чтобы сохранить владельцев файлов), проверяет читаемость архива, пишет SHA-256 и
перезапускает приложение, если оно работало. `restore.sh` проверяет SHA-256, при
необходимости сначала делает снимок текущего состояния, очищает runtime volume,
распаковывает архив и проверяет `PRAGMA integrity_check`.

Перенос на другую машину: перенести архив, `.sha256` и `.json`, на целевой машине
подготовить код и веса, выполнить `restore.sh`. Fingerprint embedding-модели
проверяет загрузчик индексов Клима; при несовпадении векторный поиск запрещён,
допустим явный BM25 после проверки артефакта. Не смешивать файлы разных снимков.

## 8. Ограниченный режим

- Нет LLM: страницы, документы, источники и поиск должны оставаться доступными;
  `/health` показывает `llm.status=disabled|unavailable` и причину.
- Нет ML: допустим BM25 с явным предупреждением; гибридный режим недоступен.
- Нет весов: `start.sh` не запускает модельные сервисы; приложение работает в
  ограниченном режиме. Не выдавать синтетические ответы за результат модели.
- `development` может включать явно помеченную заглушку UI. В профилях a100/3060ti/4b
  синтетический fallback по умолчанию выключен; включать только для демонстрации.

## 9. Версии и изменения

- Python: 3.12 (`python:3.12-slim-bookworm`, digest закреплён в Dockerfile).
- Приложение: `requirements-app.txt`; общие pydantic-пины совпадают с
  `requirements-contracts.txt`.
- ML: `torch==2.7.1`, `transformers==4.57.6`, `sentencepiece==0.2.3`.
- llama.cpp: образ `b5350` (CPU и CUDA digest в lock-файле); точное соответствие
  версии `llama.cpp` и весов подтверждает Клим.
- Веса: ревизии и SHA-256 в `config/models.lock.json`.
- CI: `.github/workflows/ci.yml` — тесты на синтетике, проверка Compose-профилей,
  сборка app-образа и проверка сохранности данных после рестарта; без GPU и весов.

Обновление версии фиксируется отдельным коммитом с обновлением lock-файла и
`docs/verification.md`. Неподтверждённые сочетания версий не объявляются рабочими.
