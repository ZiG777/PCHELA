# Развёртывание PCHELA на Debian 12

Сервис работает от системного пользователя `pchela`. Код и `.env` лежат в `/opt/pchela`, SQLite хранится в `/var/lib/pchela/shop.sqlite3`. Бот использует long polling, поэтому серверу нужен исходящий HTTPS-доступ к Telegram и настроенным платёжным провайдерам.

## Перенос одной командой из PowerShell

Для первой установки сохраните [migrate.ps1](migrate.ps1). Остановите Windows-копию бота через `Ctrl+C`. Из папки проекта запустите скрипт, указав фактический путь к нему и SSH-адрес.

```powershell
& "$env:TEMP\pchela-migrate.ps1" -Server "user@host"
```

Скрипт создаст резервную копию SQLite, перенесёт действующие настройки и запустит systemd-сервис. При необходимости укажите параметр `-Port`. SSH, SCP и sudo могут отдельно запросить пароль. Для установки нужен вход под root или право sudo.

Если первый запуск прервался до импорта базы и настроек, повторите команду с `-Resume`. Скрипт сохранит прерванную установку в отдельной папке под `/opt` и проверит новый релиз. Если серверная база, `.env` или unit уже существуют, скрипт завершится до изменения этих файлов.

## Подготовить сервер

Выполните от root. Если используете пользователя с sudo, сначала откройте оболочку через `sudo -i`.

```bash
apt-get update
apt-get install -y ca-certificates git python3 python3-venv
getent passwd pchela >/dev/null || useradd --system --user-group --home-dir /var/lib/pchela --shell /usr/sbin/nologin pchela
git clone --branch main https://github.com/ZiG777/PCHELA.git /opt/pchela
cd /opt/pchela
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
install -d -m 0700 -o pchela -g pchela /var/lib/pchela
install -m 0644 deploy/pchela.service /etc/systemd/system/pchela.service
systemctl daemon-reload
```

Если `/opt/pchela` уже содержит нужный репозиторий, обновите его командой `git -C /opt/pchela pull --ff-only origin main`. Для закрытого репозитория используйте имеющий доступ Git-ключ или распакуйте архив репозитория в `/opt/pchela`.

## Перенести настройки и базу с Windows

Остановите Windows-копию бота через `Ctrl+C` и держите её остановленной до проверки сервера. В PowerShell из папки проекта сделайте локальную копию базы. Команда берёт путь из текущей конфигурации, включая переопределение из PowerShell, и проверяет записанный снимок.

```powershell
$backup = Join-Path $env:TEMP ("pchela-" + [guid]::NewGuid() + ".sqlite3")
.\.venv\Scripts\python.exe -c "import sqlite3, sys; from pathlib import Path; from shop.config import Config; source = sqlite3.connect(Path(Config.load().database).resolve().as_uri() + '?mode=ro', uri=True); target = sqlite3.connect(sys.argv[1]); source.backup(target); result = target.execute('PRAGMA integrity_check').fetchone()[0]; target.close(); source.close(); sys.exit(0 if result == 'ok' else 'Ошибка проверки SQLite')" $backup
if ($LASTEXITCODE -ne 0) { throw "Копия SQLite не готова" }
```

Задайте `$server = "root@ВАШ_СЕРВЕР"`. Для другого SSH-логина используйте его и закройте доступ к временным файлам для остальных пользователей. Следующие команды рассчитаны на вход под root. Укажите `-p` для SSH и `-P` для SCP, если используется нестандартный порт.

```powershell
ssh $server 'install -d -m 0700 /root/pchela-import'
scp .env "${server}:/root/pchela-import/shop.env"
scp $backup "${server}:/root/pchela-import/shop.sqlite3"
```

На сервере проверьте, что копируемый `.env` содержит действующие настройки и ту же валюту, что и база. Если переменные, например `XROCKET_TOKEN`, задавались только в PowerShell, добавьте их в серверный `.env` через редактор. Unit задаёт серверный `DATABASE_PATH` сам. Следующий блок предназначен для первого импорта. Если платёжная база на сервере уже существует, сначала проверьте, какую копию использовать.

```bash
test ! -e /var/lib/pchela/shop.sqlite3 &&
  test ! -e /opt/pchela/.env &&
  install -m 0600 -o pchela -g pchela /root/pchela-import/shop.sqlite3 /var/lib/pchela/shop.sqlite3 &&
  install -m 0640 -o root -g pchela /root/pchela-import/shop.env /opt/pchela/.env
```

## Запустить и проверить

```bash
systemctl enable --now pchela.service
systemctl is-active pchela.service
journalctl -u pchela.service -n 50 --no-pager
```

Откройте Telegram и проверьте `/start`, каталог и свой баланс. Для проверки xRocket без создания счёта выполните из `/opt/pchela` команду `sudo -u pchela .venv/bin/python -m shop --check-xrocket` (из root также можно использовать `runuser -u pchela -- .venv/bin/python -m shop --check-xrocket`).

После успешной проверки удалите временную импортированную копию `shop.env` с сервера. Оригинал `.env` и резервная копия SQLite на Windows пригодятся при восстановлении. Если понадобится временно вернуть бота на Windows, сначала остановите серверный сервис и перенесите свежую копию серверной базы, если после переключения были заказы или пополнения.
