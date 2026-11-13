# Полный сервер на Windows-компьютере, VPS как HTTPS-вход

## Проверено 13 ноября 2026

SSH-аудит выполнялся только через уже настроенный `<GPU_SSH_ALIAS>`, с проверкой
известного host key; другие узлы не затронуты. Закрытые ключи и существующие `.env`
не читались. Узел: Windows 11 Pro 10.0.26200, Ryzen 7 5700X (16 логических CPU),
RAM 31.9 GiB, доступно около 20.7 GiB на момент проверки. Свободно C: ~650 GiB,
D: ~630 GiB, E: ~724 GiB. GPU RTX 3060 Ti 8192 MiB, занято 5805 MiB, driver 610.88.
Это снимок ресурсов, не проверка предельной нагрузки. Docker в PATH отсутствует.

ML отвечает на `127.0.0.1:8081`, CPU, fingerprint
`BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181:1024:normalized`,
reranker revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.
LLM: `127.0.0.1:8082/v1`, Qwen3-8B Q4_K_M, context 4096.
SHA256 существующего GGUF:
`d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
Модели не перезапускались, веса не скачивались.

Отдельный каталог подготовки:
`C:/Altropos/preflight`.
В нём создан новый `preflight-venv` на существующем Python 3.12.16, установлены
`requirements-app.txt` без кэша и сборки из исходников; `pip check` успешен.
Базовый Python:
`C:/Altropos/model-runtime/python/cpython-3.12-windows-x86_64-none/python.exe`.
Существующий ML venv не менялся; `python` в PATH указывает на Windows Store alias.

Проверены реальный HTTP на свободном localhost-порту, закрытие admin-маршрутов,
CSRF и валидация вопроса. В отдельной временной базе искусственный DOCX стал
ready/hybrid за 0.81 s; ответ generated/answered за 5.12 s,
supported=true, complete=true, conflict=false; источник и оригинал HTTP 200.
Это один успешный smoke-прогон, не оценка качества на регламентах.
Первый прогон прошёл вычисления, но не сохранил отчёт из-за отсутствия каталога
`runtime`; после создания каталога повторный прогон завершился PASS.

На машине автора: Caddy 2.11.9 `validate` успешен, отдельный локальный HTTP-прогон
подтвердил 401 с WWW-Authenticate без входа и 503 после входа при недоступном backend.
Проверки Windows backup/restore включают отказ при работающем процессе и при
восстановлении поверх существующего каталога. Linux systemd unit на VPS не проверялся.

## Архитектура и границы

`browser -> HTTPS aitropos.ru -> VPS auth/proxy -> SSH over Tailscale -> Windows localhost:18000`.
Документы, SQLite, индексы и модели находятся на Windows. Пользователи не получают
GPU-адресов или токенов. Не публиковать app на 0.0.0.0 и не открывать модельные порты.
Домен остаётся на VPS; адрес VPS и его текущий прокси пока неизвестны.

На VPS было 2.4 GiB свободного диска, 1 vCPU, 3.8 GiB RAM и существующие контейнеры.
Перед внедрением прочитать `docker ps -a`, `docker system df`, `ss -lntup`, конфигурацию
существующего веб-входа без вывода секретов. Новый Caddyfile — интеграционный шаблон,
а не разрешение заменять существующие службы. Ничего публично не включено.

## Подготовка Windows

1. Выбрать проверенный Git commit, упаковать только отслеживаемые файлы. Не переносить
   `.env`, корпуса, базы, индексы, веса, private keys. Распаковать в новый release-каталог.
2. Создать отдельное окружение (в новой сессии PowerShell из release-каталога):

   ```powershell
   ./scripts/deploy/prepare.ps1 -Python 'C:/path/to/python312/python.exe' -EnvironmentPath 'C:/altropos-envs/release1'
   ```

   Скрипт не запускает службы и отказывается работать поверх имеющегося окружения.
   Сохранить `python -m pip freeze` в закрытом журнале выпуска: транзитивные зависимости
   requirements-app пока не закреплены полностью, будущая установка не побитово идентична.
3. Скопировать `config/deploy/laptop.env.example` ВНЕ репозитория. Задать абсолютные
   постоянные DATA_DIR и REVIEW_DIR на локальном диске; ограничить NTFS-права учётной
   записью сервера/администратора. Защитить архивы так же, как исходные документы.
4. Внести фактический LLM_REVISION. Для проверенного файла допустимо значение
   `sha256:d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
   Не переносить это значение на другие веса. CPU embeddings не переводить на GPU.
5. Переносимый Tesseract проверен отдельным дополнительным прогоном: версия
   5.4.0.20240606, rus/eng/osd. Переданы только tesseract.exe, DLL, traineddata и
   конфигурации движка: 87 файлов, ZIP 63909765 байт, SHA256
   `a9b73890b63a5aa92e1561e55269501be90ce82c17146fdb4c50a3cbe1a88d35`.
   SHA на приёмной стороне совпал. PATH, реестр и настройки моделей не менялись.
   В локальном deployment.env указаны абсолютные TESSERACT_CMD и TESSDATA_PREFIX.
   Искусственный image-only PDF успешно отрендерен и распознан русским+английским
   OCR: 2506 символов, 3 units, needs_review=false, предупреждение о погрешностях OCR.
   Poppler отдельно не устанавливался: используемый PDF-путь работал с зависимостями
   приложения. Для повторной проверки после подготовки переносимого каталога:

   ```powershell
   python scripts/deploy/check_ocr.py --tesseract C:/Altropos/ocr/tesseract.exe --tessdata C:/Altropos/ocr/tessdata
   ```

   Это синтетическая проверка инфраструктуры, не разрешение публиковать реальные
   сканы. Корпус и approved snapshots не переносились. Approval fingerprints зависят
   от окружения/путей; миграция рабочего DATA_DIR — отдельный согласованный offline
   backup/restore с проверкой совместимости, без обхода fingerprint и без override ready.
6. Приватный тест без рабочего корпуса:

   ```powershell
   C:/altropos-envs/release1/Scripts/python.exe scripts/deploy/smoke.py C:/private/laptop.env
   New-Item -ItemType Directory -Path runtime -Force
   C:/altropos-envs/release1/Scripts/python.exe scripts/check_rag_cycle.py C:/private/laptop.env
   ```

   Первый скрипт запускает и завершает только свой дочерний процесс; использует новую
   временную базу. Второй также создаёт искусственный DOCX и новую временную базу,
   обращается к существующим моделям и пишет отчёт в `runtime/api-cycle-live.json`.
7. Запуск reader:

   ```powershell
   C:/altropos-envs/release1/Scripts/python.exe scripts/deploy/serve.py --env C:/private/laptop.env --port 18000
   ```

   Только loopback, один worker, без reload. Windows byte-range lock на DATA_DIR
   не позволяет второму этому launcher работать с тем же хранилищем.
   Не обходить его прямым uvicorn и не запускать другую копию backend на той же базе.
8. Администрирование: остановить reader, запустить ту же команду с `--admin --port 18001`.
   Этот порт доступен только владельцу через отдельный приватный SSH-туннель;
   В ЭТО ВРЕМЯ ОСТАНОВИТЬ VPS-туннель к этому порту, иначе администраторский backend
   попадёт под публичный reverse proxy. Безопаснее использовать ДРУГОЙ localhost-порт
   для `--admin`, например 18001, который никогда не фигурирует в VPS upstream.
   Launcher запрещает admin на порту 18000; нестандартный порт публичного туннеля тоже
   нельзя использовать для admin. Reader и admin с одним DATA_DIR взаимоисключаются.
   Затем остановить admin и вернуть reader.

Рекомендуемый административный запуск: `--admin --port 18001`. Автозапуск через Task
Scheduler настраивается отдельно после пилота: отдельная учётная запись, запуск Python
с абсолютными аргументами и рабочим каталогом release, запрет второй копии задачи,
перезапуск после сбоя, ограниченные журналы. Сон/питание и существующие задачи сейчас
не менялись. Не использовать интерактивный SSH-сеанс как постоянный supervisor.

Готовы `scripts/deploy/supervise.ps1` и `write-logon-task.ps1`. Первый запускает
только свой reader, ждёт завершения и повторяет запуск через 10 s; не трогает модели.
Пример: `./scripts/deploy/supervise.ps1 -Python C:/altropos-envs/release1/Scripts/python.exe -EnvFile C:/private/laptop.env -StateDirectory C:/altropos-supervisor`.
Остановка: создать файл `C:/altropos-supervisor/stop.request`; supervisor завершит
только свой дочерний процесс и выйдет. Для повторного старта явно удалить этот файл.
`write-logon-task.ps1` принимает те же пути, `-TaskUser DOMAIN/user` и `-OutputXml`;
он ТОЛЬКО записывает XML для просмотра владельцем. После отдельного ручного импорта
задача стартует при входе указанного пользователя, без повышения прав; это не запуск
до входа пользователя. Автозапуск в Task Scheduler не регистрировался.

## Публичная граница

ReaderGateway — дополнительная fail-closed ASGI-обёртка, основной код приложения не меняет.
Она не заменяет авторизацию: к её порту разрешён только доступ с VPS через приватный канал.
GET/HEAD: корень, static, фрагмент источника и оригинал документа; GET `/health` возвращает
только санитизированные статусы. Полные списки документов/jobs, review, metadata,
загрузка, OpenAPI и любые новые неизвестные маршруты закрыты с 403.
POST разрешён только `/api/ask`: точный Origin `https://aitropos.ru`, Content-Type JSON,
тело до 32 KiB, общий deadline получения тела 10 s; один вопрос одновременно,
минимум 2 s между началами вопросов на процесс. Иначе 403/408/413/429.
Это ограниченный пилот: полноценные роли и отдельные лимиты на пользователей не реализованы.
Общий лимит может занять один авторизованный участник. Документы и review управляются приватно.

## VPS: только после получения реквизитов

1. Получить VPS IP, существующую схему портов/контейнеров, доступ к DNS, список участников.
   Подготовить изменение существующего прокси и rollback его конфигурации.
2. Подключить VPS к tailnet; ограничить grants VPS -> GPU SSH tcp:22. Проверить другие
   разрешения, чтобы не осталось широкого allow-all. Не менять чужие правила вслепую.
3. Под отдельным пользователем VPS подготовить собственный SSH-ключ и known_hosts.
   Fingerprint сверить по уже доверенному каналу; StrictHostKeyChecking=yes.
   Не копировать закрытый ключ пользователя с его компьютера. Настройка нового ключа
   на Windows — отдельное изменение после проверки администратором.
4. Шаблон `config/deploy/altropos-tunnel.service`: локальный порт VPS 127.0.0.1:18000
   переадресуется на Windows 127.0.0.1:18000. Keepalive/restart восстанавливают туннель.
   Проверить свободный порт и `systemd-analyze verify` на целевом Debian перед установкой.
   Документированные SSH alias использует пользователя `<WINDOWS_USER>`; не угадывать другой логин.
5. Сгенерировать отдельные имена/хеши участников командой `caddy hash-password`.
   Шаблон поддерживает одного участника через ALTROPOS_READER_USER/HASH; для нескольких
   добавить отдельные записи basic_auth. Не хранить пароли/хеши в Git. HTTPS обязателен.
6. Внести upstream в существующий reverse proxy. Если это Caddy — интегрировать
   `config/deploy/Caddyfile`, validate, сохранить старый файл, затем reload.
   Caddy удаляет Authorization перед передачей на Windows. Недоступный backend — 503;
   отсутствие входа — 401 с корректным challenge. Не включать чувствительные access logs.
7. Проверить A/AAAA aitropos.ru и сертификат. DNS/firewall/публичный запуск до этого
   не менялись; IP VPS и занятость 80/443 не предполагаются известными.

Прямой Tailscale Serve — альтернативный транспорт, не нужен одновременно с SSH-туннелем.
Если выбран, разрешить VPS только reader-порт, не admin и не модели; Funnel не использовать.

Если существующий Caddy работает в Docker bridge, его 127.0.0.1 относится к контейнеру.
Upstream должен указывать на проверенный адрес gateway этого bridge, а SSH -L должен
слушать именно этот адрес VPS. Проверить фактическую сеть через docker inspect;
не использовать 0.0.0.0 или публичный IP. Доступ к этому порту возможен другим контейнерам
в той же сети: это граница доверия, которую следует учитывать при изоляции приложений.
Оригинал Caddyfile сохранить; менять только нужный доменный блок. При bind-mount одного
файла запись выполнять с сохранением inode, иначе контейнер может продолжить видеть
старую версию после атомарной замены файла на хосте. Выполнить validate перед reload.

Для Cloudflare-проксирования шаблон использует REQUEST_TIMEOUT=25: общий deadline
вопроса 4x25=100 s. Caddy ждёт заголовки 110 s, что оставляет запас до стандартного
Cloudflare Proxy Read Timeout 125 s. Тарифные настройки не считаются проверенными;
не увеличивать таймауты без проверки внешней цепочки. Более долгие задачи требуют
асинхронного API/опроса статуса, а не бесконечного ожидания браузера.

## Копии, обновление, приёмка

Остановить reader/admin; затем:

```powershell
python scripts/deploy/backup.py C:/altropos-data D:/private-backups/before-update.zip
python scripts/deploy/restore.py D:/private-backups/before-update.zip C:/altropos-restored-new
```

Использовать полный путь к подготовленному Python. Backup блокирует DATA_DIR,
выполняет WAL checkpoint и integrity_check, архивирует всё хранилище и пишет SHA256.
Restore проверяет hash/CRC/пути и integrity_check, разрешает только НОВУЮ директорию.
После проверки восстановления вручную изменить DATA_DIR в закрытой конфигурации.
Review-каталог хранится отдельно: при необходимости делать отдельную приватную копию.
Копии на другом диске этой машины не защищают от потери всей машины: нужна внешняя копия.

При обновлении: backup, новый release и проверенное окружение, smoke на искусственных
данных, остановка старого reader, переключение release, старт. Не запускать два worker.
Откат кода требует совместимой схемы БД; иначе восстановить согласованный снимок в новый DATA_DIR.

До публикации проверить: вход/401, анонимный original/источник закрыт, авторизованное
чтение работает, metadata/upload/review закрыты, cross-origin POST ask закрыт, обычный
вопрос с цитатами работает; отключение туннеля даёт 503, возврат связи восстанавливает
новые вопросы, данные переживают рестарт, backup действительно восстанавливается.
Не обещать доступность при сне/выключении Windows и не выдавать smoke за нагрузочный тест.

## Официальные источники

- [Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
- [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy matchers](https://caddyserver.com/docs/caddyfile/matchers)
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [Tailscale grants](https://tailscale.com/docs/reference/syntax/grants)
- [Tesseract installation and language data](https://tesseract-ocr.github.io/tessdoc/Installation.html)
- [Cloudflare 524 and default read timeout](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
