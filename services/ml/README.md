# Локальные модели

Реализованы только адаптеры и BGE HTTP-сервис. Поиск, orchestration, API приложения,
переиндексация и переключение профиля сюда не входят. Общие schemas, Settings,
interfaces и requirements не изменены. Внешних AI-провайдеров и автоматического
fallback нет. Адрес сервиса задаётся явно (loopback, SSH-туннель или доверенная LAN).
HTTP proxy из окружения и перенаправления отключены; сервис не выставлять в Интернет.

## Предложение по отдельному окружению инференса

Для реального BGE нужны `torch` (сборка под CPU/CUDA выбранной машины) и
`transformers` с AutoModel/AutoModelForSequenceClassification. Предлагаемый диапазон
для отдельного ML requirements после согласования: `torch>=2.2,<3`,
`transformers>=4.44,<5`; точные версии следует закрепить после аппаратного smoke test.
Они **не добавлены** в общие зависимости и не устанавливались в рамках этой работы.
Транспорт и тесты требуют только существующих requirements-contracts.txt.

Веса обоих BGE должны уже находиться в HF-кеше. Сервис использует
`local_files_only=True`, `trust_remote_code=False`, полные 40-символьные commit SHA.
При отсутствии кеша, CUDA или зависимостей запуск завершится понятной ошибкой,
без скачивания и без перехода на CPU. При импорте модуля модели не загружаются.
Проверьте существующий EMBEDDING_REVISION перед развёртыванием.

Пример PowerShell (SHA заменить проверенными ревизиями локальных весов):

```powershell
$env:PROFILE = '3060ti'
$env:ML_ENABLED = 'true'
$env:RERANKER_REVISION = '<40-character-commit-sha>'
python -m services.ml.server --host 127.0.0.1 --port 8081
```

`POST /embed`: JSON EmbeddingRequest → EmbeddingResponse из navigator/schemas.py.
Векторы возвращаются в порядке texts: dense CLS + L2-нормализация, 1024 координаты,
fingerprint модели/ревизии. purpose не меняет представление: BGE-M3 не требует
разных префиксов запроса и документа.

`POST /rerank`: RerankRequest → RerankResponse; каждый ID ровно один раз,
в порядке входа, score — сырой logit (не вероятность). Дубли ID запрещены.
Batch HTTP ограничен схемами (64 текста / 40 фрагментов); инференс идёт порциями
по четыре. Тексты свыше 8192 токенов не обрезаются молча: запрос отклоняется.
Одновременно выполняется одна BGE-операция; занятый сервис отвечает 503.
Ошибки не содержат тела запроса, локальных путей и traceback.

## llama.cpp

Нужен локально собранный llama-server с поддержкой `/tokenize`, `/apply-template`,
`/v1/chat/completions`, JSON Schema и Jinja-параметра `enable_thinking`.
Ревизию бинарника, GGUF-файла и его SHA-256 фиксирует оператор при развёртывании.
LLM_REVISION — обязательная декларация закреплённой ревизии; HTTP API llama.cpp
не подтверждает SHA весов. Клиент не выдаёт эту декларацию за удалённую аттестацию.
`--alias` должен совпадать с Settings.llm_model. Использовать `--parallel 1`:

```text
llama-server -m <local-model.gguf> --host 127.0.0.1 --port 8082 --parallel 1 --jinja --ctx-size <context> --alias <llm_model>
```

| PROFILE | llm_model / Q4_K_M | context | Материалы | Выход | Фрагменты | BGE |
|---|---|---:|---:|---:|---:|---|
| a100 | Qwen2.5-14B-Instruct-GGUF-Q4_K_M | 16384 | 8000 | 1536 | 8 | CUDA |
| 3060ti | Qwen3-8B-GGUF-Q4_K_M | 4096 | 2200 | 768 | 4 | CPU |
| 4b | Qwen3-4B-GGUF-Q4_K_M | 4096 | 2200 | 768 | 4 | CPU |

Это значения существующих Settings; явные overrides сохраняются. Число GPU-слоёв
выбирается оператором после измерения памяти. Вместимость 8B на 3060 Ti не обещается.
Для включения клиента задать LLM_ENABLED=true и проверенную LLM_REVISION.
Qwen3 получает `chat_template_kwargs.enable_thinking=false`, `reasoning_effort=none`;
те же параметры передаются при построении шаблона. Неподдерживаемый API даёт
явную ошибку, без перехода на приблизительный подсчёт токенов или обычный текст.
Клиент проверяет бюджет материалов и полный шаблон (включая вопрос, JSON Schema,
черновик verify и инструкции) плюс max_output_tokens фактическим токенизатором сервера.
Сервер должен быть запущен с контекстом не меньше Settings.context_size.

Один экземпляр LLMClient на модель. Внешняя цепочка должна удерживать его общую
блокировку на весь цикл генерации, проверки и возможного исправления:

```python
async with client.sequence():
    draft = await client.generate(generation_request)
    verification = await client.verify(verification_request)
```

Без sequence отдельные вызовы тоже сериализуются. Ретраев нет: ими управляет
будущий orchestration. JSON без markdown, конечные числа, строгая структура Draft /
Verification, finish_reason=stop, известные ID и точные цитаты проверяются клиентом.
Проверка смысла — отдельный verify-вызов; успешная структура не доказывает смысл.
Ожидание блокировки ограничено request_timeout; каждый вызов LLM также имеет общий
дедлайн на токенизацию, шаблон и генерацию. Отмена HTTP не гарантирует остановку
уже запущенного серверного вычисления, поэтому --parallel 1 обязателен.

## Проверки и ограничения

`python -m pytest -q` проверяет сетевые отказы и ответы на искусственных данных,
без torch/transformers, весов и GPU. На Windows restricted sandbox может блокировать
создание asyncio socketpair; сетевые тесты нужно запускать с доступом к loopback.

Аппаратные испытания **не выполнялись**: в доступном Python 3.11 нет torch и
transformers, команда nvidia-smi недоступна, доступы к A100 не предоставлены,
локальные ревизии весов не подтверждены. Веса не скачивались. Время инференса,
VRAM/RAM, качество и реальная совместимость конкретной сборки llama.cpp не измерены.
Отдельный smoke test должен проверить загрузку закреплённых весов, fingerprint,
нормы embeddings, logits reranker, generate→verify и потребление памяти для каждого
из трёх профилей. CPU-сетевые тесты не заменяют этот этап.

Источники формата и механизма:
[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[BGE-M3](https://huggingface.co/BAAI/bge-m3),
[BGE reranker](https://huggingface.co/BAAI/bge-reranker-v2-m3).
