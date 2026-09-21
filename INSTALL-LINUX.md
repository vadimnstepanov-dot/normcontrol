# Установка на Linux

Linux поддерживает два режима:

- `portal-only` — портал, пользователи, очередь и отчёты на VPS; проверку выполняет Windows-worker с GPU;
- `full` — портал и локальный движок на Linux; принимаются `.docx`, а модель доступна через локальный OpenAI-совместимый API.

Формат `.doc`, Microsoft Word COM, сверка нумерации через Word и удержание Windows от сна на Linux недоступны. Для старых `.doc` используйте Windows-worker или заранее преобразуйте их в `.docx` в доверенной среде.

## 1. Требования

Пример для Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Проверьте версию: `python3 --version`. Поддерживаются Python 3.11 и 3.12.

## 2. Получение проекта

```bash
git clone https://github.com/vadimnstepanov-dot/normcontrol.git
cd normcontrol
```

## 3. Автоматическая установка

Только портал:

```bash
bash scripts/install-linux.sh --portal-only
```

Полная установка:

```bash
bash scripts/install-linux.sh --full
```

Для нестандартного пути Python:

```bash
PYTHON_BIN=/opt/python3.12/bin/python3 bash scripts/install-linux.sh --full
```

Сценарий создаёт `.venv`, устанавливает необходимые зависимости и при полном режиме создаёт локальный `sto_rag/data/nc5/config.json` из безопасного примера.

## 4. Проверка установки

```bash
.venv/bin/python -m pip check
APP_DEBUG=1 APP_DATA=/tmp/normcontrol-check \
  .venv/bin/python normcontrol-web/manage.py check
```

Тесты портала:

```bash
APP_DEBUG=1 APP_DATA=/tmp/normcontrol-tests \
  .venv/bin/python normcontrol-web/manage.py test portal
```

## 5. Портал в режиме разработки

```bash
export APP_DEBUG=1
export APP_DATA="$PWD/normcontrol-web/data"
.venv/bin/python normcontrol-web/manage.py migrate
.venv/bin/python normcontrol-web/manage.py createsuperuser
.venv/bin/python normcontrol-web/manage.py runserver 127.0.0.1:8110
```

Откройте `http://127.0.0.1:8110/normcontol/`.

## 6. Производственный портал

Создайте закрытый файл `/etc/normcontrol.env`, доступный только системному пользователю сервиса:

```bash
APP_SECRET=<long-random-django-secret>
APP_CRYPT_KEY=<fernet-key>
APP_HOSTS=normcontrol.example.org
APP_ORIGINS=https://normcontrol.example.org
APP_DATA=/var/lib/normcontrol
APP_NAME=Нормоконтроль
NORMCONTROL_WORKER_TOKEN=<random-token-at-least-32-characters>
```

Сгенерировать Fernet-ключ:

```bash
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Не добавляйте `/etc/normcontrol.env` в Git. Ограничьте доступ:

```bash
sudo chmod 600 /etc/normcontrol.env
```

Подготовьте базу и статические файлы:

```bash
set -a
source /etc/normcontrol.env
set +a
.venv/bin/python normcontrol-web/manage.py migrate
.venv/bin/python normcontrol-web/manage.py collectstatic --noinput
.venv/bin/python normcontrol-web/manage.py createsuperuser
```

Пример запуска Gunicorn из корня проекта:

```bash
set -a; source /etc/normcontrol.env; set +a
.venv/bin/gunicorn --chdir normcontrol-web --bind 127.0.0.1:8110 portal.wsgi:application
```

## 7. Пример systemd

Замените `/opt/normcontrol` и пользователя `normcontrol` на фактические значения:

```ini
[Unit]
Description=Normcontrol portal
After=network.target

[Service]
Type=simple
User=normcontrol
Group=normcontrol
WorkingDirectory=/opt/normcontrol
EnvironmentFile=/etc/normcontrol.env
ExecStart=/opt/normcontrol/.venv/bin/gunicorn --chdir normcontrol-web --bind 127.0.0.1:8110 portal.wsgi:application
Restart=on-failure
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

После сохранения в `/etc/systemd/system/normcontrol.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now normcontrol
sudo systemctl status normcontrol
```

## 8. Reverse proxy

Портал использует префикс `/normcontol/`. Пример фрагмента Nginx:

```nginx
location /normcontol/static/ {
    alias /opt/normcontrol/normcontrol-web/static-collected/;
}

location /normcontol/ {
    proxy_pass http://127.0.0.1:8110;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 120m;
    proxy_read_timeout 360s;
}
```

Настройте HTTPS. Публиковать порт llama.cpp напрямую в интернет не требуется.

## 9. Полный режим с локальной LLM

Скопируйте и настройте [scripts/start-llama-linux.example.sh](scripts/start-llama-linux.example.sh) либо запустите совместимый `llama-server` самостоятельно. Сервер должен слушать только loopback, например `127.0.0.1:8082`. Проверка подключения:

```bash
export PYTHONPATH="$PWD/sto_rag"
.venv/bin/python sto_rag/normcontrol_v5.py probe
```

Локальный UI:

```bash
export PYTHONPATH="$PWD/sto_rag"
.venv/bin/python sto_rag/normcontrol_v5.py serve
```

На Linux движок обрабатывает `.docx`. Для использования готового нормативного каталога перенесите каталог `sto_rag/data` с доверенной Windows-машины вместе с исходными редакциями СТО. Автоматическая первичная сверка Word-нумерации выполняется на Windows по [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md).

## 10. Worker на Linux

Создайте `worker.env` по образцу и заполните HTTPS-адрес портала и общий токен:

```bash
cp config/worker.env.example worker.env
export PYTHONPATH="$PWD/sto_rag"
.venv/bin/python -m nc5.launch_worker ./worker.env
```

Linux-worker принимает `.docx`. Пакет с `.doc` завершится понятной ошибкой преобразования; для таких файлов используйте Windows-worker.

## 11. Обновление

```bash
sudo systemctl stop normcontrol
git pull --ff-only
bash scripts/install-linux.sh --portal-only
set -a; source /etc/normcontrol.env; set +a
.venv/bin/python normcontrol-web/manage.py migrate
.venv/bin/python normcontrol-web/manage.py collectstatic --noinput
sudo systemctl start normcontrol
```

Перед обновлением сохраните `APP_DATA` и, для полного режима, `sto_rag/data/nc5`.
