#!/usr/bin/env bash
set -euo pipefail

stage=${1:?Pass the private upload directory}
resume=${2:-}
if [[ $EUID -ne 0 || ! $stage =~ ^/tmp/pchela-import\.[a-zA-Z0-9]{8}$ || ! -d $stage ]]; then
    echo 'Run the installer as root with the private upload directory.' >&2
    exit 1
fi
trap 'rm -f -- "$stage/shop.env"' EXIT
umask 022
. /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 12 ]]; then
    echo 'This installer expects Debian 12.' >&2
    exit 1
fi

app=/opt/pchela
db=/var/lib/pchela/shop.sqlite3
unit=/etc/systemd/system/pchela.service
for path in "$app/.env" "$db" "$unit"; do
    if [[ -e $path || -L $path ]]; then
        printf 'Existing deployment file found: %s\n' "$path" >&2
        exit 1
    fi
done
if [[ -e $app || -L $app ]]; then
    if [[ $resume != --resume || -L $app || ! -d $app || ! -f $app/shop/__main__.py || ! -f $app/deploy/pchela.service ]]; then
        printf 'Existing deployment directory found: %s\n' "$app" >&2
        exit 1
    fi
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates python3 python3-venv unzip
if ! getent passwd pchela >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/pchela --shell /usr/sbin/nologin pchela
fi

source_dir=$(mktemp -d /opt/pchela-build.XXXXXXXX)
trap 'rm -f -- "$stage/shop.env"; rm -rf -- "$source_dir"' EXIT
unzip -q "$stage/release.zip" -d "$source_dir"
source="$source_dir/PCHELA-main"
test -f "$source/shop/__main__.py"
test -f "$source/deploy/pchela.service"
chmod -R u=rwX,go=rX "$source"
if [[ -d $app ]]; then
    backup=$(mktemp -d /opt/pchela-interrupted.XXXXXXXX)
    mv -T "$app" "$backup/app"
    printf 'Saved the interrupted installation to %s/app\n' "$backup"
fi
mv -T "$source" "$app"
cd "$app"
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v

install -d -m 0700 -o pchela -g pchela /var/lib/pchela
install -m 0600 -o pchela -g pchela "$stage/shop.sqlite3" "$db"
install -m 0640 -o root -g pchela "$stage/shop.env" "$app/.env"
runuser -u pchela -- env DATABASE_PATH="$db" .venv/bin/python - <<'PY'
import asyncio
import sqlite3
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from shop.config import Config

config = Config.load()
with sqlite3.connect(Path(config.database).as_uri() + "?mode=ro", uri=True) as db:
    if db.execute("SELECT value FROM settings WHERE name = 'currency'").fetchone() != (config.currency,):
        raise SystemExit("The deployed SQLite currency differs from SHOP_CURRENCY.")
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise SystemExit("The deployed SQLite database failed its integrity check.")

async def check_telegram():
    async with Bot(config.token, session=AiohttpSession(timeout=8)) as bot:
        info = await bot.get_me()
        print(f"Telegram API reachable. Bot: @{info.username}")

try:
    asyncio.run(check_telegram())
except Exception as error:
    raise SystemExit(f"Telegram API check failed ({type(error).__name__}).") from None
PY

install -m 0644 "$app/deploy/pchela.service" "$unit"
systemctl daemon-reload
systemctl enable --now pchela.service
for ((attempt = 0; attempt < 20; attempt++)); do
    pid=$(systemctl show --property=MainPID --value pchela.service)
    if [[ $pid != 0 ]] && systemctl is-active --quiet pchela.service &&
       journalctl -u pchela.service "_PID=$pid" --no-pager -o cat | grep -Fq 'Run polling for bot'; then
        echo 'PCHELA is active and Telegram polling has started.'
        exit 0
    fi
    sleep 1
done
echo 'Polling did not start. Check journalctl -u pchela.service on the server.' >&2
exit 1
