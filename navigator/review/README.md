# Локальная проверка распознавания

Этот модуль не индексирует, не публикует и не меняет БД. Все выходные файлы
следует хранить в локальном runtime, вне Git. Проверяющий читает оригинал,
извлечённые блоки, сноски, предупреждения и итоговые фрагменты в `index.html`.
Решение по умолчанию — `pending`. HTML — неизменяемый снимок; актуальное решение
хранится в `review.json` и выводится командой `show`.

```python
from pathlib import Path
from navigator.review import prepare_document, write_index, load_approved_package

out = Path("runtime/review-cycle-1")
packages = [prepare_document(source, out, group="initial") for source in sources]
write_index(out)  # открыть runtime/review-cycle-1/index.html

# Если разбор уже выполнен, передать parsed=parsed: повторного OCR не будет.
# Подготовивший Parsed отвечает за его соответствие переданному оригиналу.
# Решение записывается только после явного ответа пользователя.
```

CLI поддерживает несколько источников в одном запуске:

```text
python -B scripts/corpus_review.py prepare synthetic-a.pdf synthetic-b.docx --out runtime/review-cycle-1 --group initial
python -B scripts/corpus_review.py show runtime/review-cycle-1/SHA_PREFIX
python -B scripts/corpus_review.py decision runtime/review-cycle-1/SHA_PREFIX --decision approved --reviewer USER --comment "Явное решение пользователя"
```

`record_decision(package_dir, decision, *, reviewer, comment='')` принимает
`approved`, `rejected`, `excluded`; непустой reviewer обязателен. Не вызывает
внешние службы. Нельзя вызывать функцию вместо решения пользователя.
`needs_review`, пустые фрагменты и ошибки PDF-превью запрещают одобрение;
для исправления нужно создать новый пакет после исправленного разбора.

`prepare_document(source, out_root, *, parsed=None, group='initial')` возвращает
`{package_dir, review, manifest}`. Каталог — `out_root/sha256[:16]`.
Существующий каталог не перезаписывается: новый цикл требует нового `out_root`.
Он содержит `original.EXT`, `snapshot.json` (все поля Parsed и результат build),
`fragments.json`, `readable.txt`, `index.html`, PDF PNG, `manifest.json`, `review.json`.
PDF-превью строятся локально pypdfium2, с поворотом/ошибкой каждой страницы.
XLSX использует реальные номера строк и столбцов, включая пустые позиции;
для сетки свыше 200000 ячеек выводится адресный список без расширения пропусков.
Снимок сохраняет исходные структурные блоки без изменений.

`load_approved_package(package_dir)` проверяет решение, SHA оригинала,
канонические digest снимка/блоков/фрагментов, все артефакты просмотра,
код парсеров/схем/review, версии библиотек и доступные локальные OCR assets.
Изменение диагностики входит в изменение снимка или манифеста и инвалидирует
решение. Обновление кода/окружения требует нового цикла проверки, без повторного
одобрения автоматически. Проверка не вызывает OCR, LLM, embeddings или GPU.

Успешный результат:

```python
approved = load_approved_package(package_dir)
document_payload = approved["document"]  # dict; status=processing, никогда ready
fragments = approved["fragments"]       # list[Fragment], точные проверенные тексты/locator
original = approved["source_original"] # абсолютный Path на snapshot оригинала
decision = approved["review"]
```

Публикующий код должен отдельно скопировать original в хранилище, использовать
эти Fragment без пересборки/повторного OCR, установить путь хранилища и провести
обычную согласованную публикацию. Идентификаторы включают digest извлечения:
новое извлечение меняет ID даже при неизменном оригинале. Одобрение OCR не
подтверждает `metadata.reviewed`, сроки действия или юридическую актуальность.

Пакеты защищены контрольными суммами, не цифровой подписью. Каталог runtime
должен быть доверенным: пользователь с правом перезаписать всё содержимое
может пересоздать манифест и решение. Для параллельных писателей блокировки
не предусмотрены; подготовку, решения и публикацию следует выполнять последовательно.
