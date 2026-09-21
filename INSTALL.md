# Установка и настройка

## 1. Варианты развёртывания

Для одной рабочей станции достаточно локального движка и страницы на `127.0.0.1:8096`.

Для нескольких пользователей применяйте два узла:

1. HTTPS-портал принимает документы, управляет пользователями, очередью и отчётами.
2. Worker на компьютере с GPU забирает следующее задание, запускает локальную LLM и возвращает только состояние и результат.

Порталу не нужна видеокарта. LLM API не следует публиковать в интернет; worker должен обращаться к нему по `127.0.0.1`.

## 2. Требования

### Рабочая станция

- Windows 10/11 x64;
- Python 3.11 или 3.12 x64;
- `llama.cpp` с OpenAI-совместимым `llama-server`;
- GGUF-модель, подходящая к доступной VRAM;
- Microsoft Word для `.doc`, визуальных свойств Word и сверки автоматической нумерации;
- свободное место для модели, нормативных источников, индекса и отчётов.

Файлы `.docx` читаются напрямую. Для `.doc` система использует автоматизацию Microsoft Word и создаёт рабочую копию `.docx`.

### Портал

- Linux или Windows с Python 3.11+;
- HTTPS reverse proxy для производственной установки;
- постоянный каталог для SQLite, загруженных документов и отчётов.

## 3. Установка локального движка

```powershell
git clone https://github.com/vadimnstepanov-dot/normcontrol.git
Set-Location .\normcontrol
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

Сценарий создаёт `.venv` и устанавливает зависимости локального движка и портала. Ручной эквивалент:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\sto_rag\nc5\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .\normcontrol-web\requirements.txt
```

## 4. Запуск llama.cpp

Скопируйте [scripts/start-llama.example.bat](scripts/start-llama.example.bat), укажите пути к `llama-server.exe` и модели, затем запустите копию. Пример использует один параллельный слот, Flash Attention, `batch-size 2048` и `ubatch-size 512`.

Проверьте API:

```powershell
Invoke-RestMethod http://127.0.0.1:8082/v1/models
```

Размер контекста должен помещаться в VRAM вместе с весами и KV-кэшем. Начинайте с 20–32K и увеличивайте после замера на реальных документах. Слишком большой контекст способен снизить скорость и вызвать перенос слоёв или KV-кэша в RAM.

## 5. Конфигурация движка

```powershell
New-Item -ItemType Directory -Force .\sto_rag\data\nc5 | Out-Null
Copy-Item .\config\config.example.json .\sto_rag\data\nc5\config.json
```

Основные параметры:

| Поле | Назначение |
|---|---|
| `endpoint` | базовый адрес OpenAI-совместимого API |
| `model` | имя модели, передаваемое API |
| `context` | доступное контекстное окно в токенах |
| `output` | предел ответа одного вызова |
| `timeout` | тайм-аут вызова модели в секундах |
| `port` | порт локального интерфейса |
| `formatting` | `off` или `xml` для проверки свойств Word |

Если API требует ключ, задайте его только в переменной окружения:

```powershell
$env:NORMCONTROL_LLM_API_KEY = "<private-api-key>"
```

## 6. Подготовка RAG по СТО

Репозиторий не содержит нормативных документов. Поместите разрешённые к использованию файлы `.docx` в корень клона. Для стандартов с автоматической нумерацией сначала сохраните эталонные подписи абзацев через Word:

```powershell
powershell -ExecutionPolicy Bypass -File .\sto_rag\verify_word.ps1
.\.venv\Scripts\python.exe .\sto_rag\rag.py build
$env:PYTHONPATH = "$PWD\sto_rag"
.\.venv\Scripts\python.exe .\sto_rag\normcontrol_v5.py catalog
.\.venv\Scripts\python.exe .\sto_rag\normcontrol_v5.py catalog-audit
```

После замены любого нормативного файла повторите все четыре команды. Каталог хранит SHA-256 источника и не использует изменившийся файл без повторной сборки.

Для проверки по инструкции по делопроизводству добавьте её как отдельный разрешённый источник. Применимость общих требований должна уступать специальному СТО при конфликте.

## 7. Локальная работа

```powershell
$env:PYTHONPATH = "$PWD\sto_rag"
.\.venv\Scripts\python.exe .\sto_rag\normcontrol_v5.py probe
.\.venv\Scripts\python.exe .\sto_rag\normcontrol_v5.py serve
```

Интерфейс доступен на `http://127.0.0.1:8096`. Во время активной проверки процесс удерживает Windows от перехода в сон; после завершения системные параметры сна снова действуют.

## 8. Локальная проверка из командной строки

```powershell
$env:PYTHONPATH = "$PWD\sto_rag"
.\.venv\Scripts\python.exe .\sto_rag\normcontrol_v5.py run "C:\Documents\ТЗ.docx"
```

Несколько путей в одной команде образуют связанный комплект, для которого выполняется междокументная проверка.

## 9. Запуск портала для разработки

```powershell
$env:APP_DEBUG = "1"
$env:APP_DATA = "$PWD\normcontrol-web\data"
.\.venv\Scripts\python.exe .\normcontrol-web\manage.py migrate
.\.venv\Scripts\python.exe .\normcontrol-web\manage.py createsuperuser
.\.venv\Scripts\python.exe .\normcontrol-web\manage.py runserver 127.0.0.1:8110
```

Откройте `http://127.0.0.1:8110/normcontol/`. Написание `/normcontol/` сохранено как текущий URL приложения.

## 10. Производственная конфигурация портала

Создайте секреты вне каталога репозитория:

```bash
export APP_SECRET='<long-random-django-secret>'
export APP_CRYPT_KEY='<fernet-key>'
export APP_HOSTS='normcontrol.example.org'
export APP_ORIGINS='https://normcontrol.example.org'
export APP_DATA='/var/lib/normcontrol'
export APP_NAME='Нормоконтроль'
export NORMCONTROL_WORKER_TOKEN='<random-token-at-least-32-characters>'
```

Ключ Fernet можно создать локально:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Подготовьте приложение:

```bash
python normcontrol-web/manage.py migrate
python normcontrol-web/manage.py collectstatic --noinput
python normcontrol-web/manage.py createsuperuser
gunicorn --chdir normcontrol-web --bind 127.0.0.1:8110 portal.wsgi:application
```

Reverse proxy должен передавать `/normcontol/` и `/normcontol/static/` в приложение, сохранять исходные заголовки `Host` и `X-Forwarded-Proto`, ограничивать размер загрузки и использовать HTTPS.

## 11. Подключение worker к порталу

На компьютере с GPU создайте приватный файл по образцу:

```powershell
Copy-Item .\config\worker.env.example .\worker.env
```

Заполните `NORMCONTROL_PORTAL_URL` и тот же `NORMCONTROL_WORKER_TOKEN`, который настроен на портале. Затем:

```powershell
$env:PYTHONPATH = "$PWD\sto_rag"
.\.venv\Scripts\python.exe -m nc5.launch_worker .\worker.env
```

Worker принимает только `.doc`/`.docx`, сверяет SHA-256 загруженного файла и не преобразует серверные идентификаторы в произвольные локальные пути. Для внешнего портала требуется HTTPS.

## 12. Обновление и резервное копирование

Перед обновлением остановите worker и портал, создайте копию каталога `APP_DATA` и `sto_rag/data/nc5`, затем:

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r .\sto_rag\nc5\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .\normcontrol-web\requirements.txt
.\.venv\Scripts\python.exe .\normcontrol-web\manage.py migrate
```

Нормативные источники и индекс резервируйте совместно: отчёт должен оставаться связанным с той редакцией источника, по которой он сформирован.

## 13. Диагностика

- `probe` не проходит: проверьте порт llama.cpp, имя модели и отсутствие системного proxy для localhost.
- Каталог не строится: выполните `verify_word.ps1` после последнего изменения `.docx` и убедитесь, что Word установлен.
- `.doc` не открывается: проверьте Microsoft Word и отсутствие диалога защищённого просмотра.
- Пакет остаётся в очереди: проверьте worker, совпадение токена и доступность HTTPS-портала.
- Контекст переполняется: уменьшите размер структурного блока или `output`; не заменяйте пакетную обработку передачей всего документа в один запрос.
- Ноль замечаний при частичной проверке: изучите неподтверждённые элементы и ошибки проходов; такой результат не подтверждает соответствие.

## 14. Тесты

```powershell
$env:PYTHONPATH = "$PWD\sto_rag"
.\.venv\Scripts\python.exe -m unittest discover -s .\sto_rag\nc5\tests

$testData = Join-Path $env:TEMP "normcontrol-web-tests"
$env:APP_DEBUG = "1"
$env:APP_DATA = $testData
.\.venv\Scripts\python.exe .\normcontrol-web\manage.py test portal
```

Не используйте производственную базу данных для тестов.
