param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9.][a-zA-Z0-9.-]*$')]
    [string]$Server,
    [ValidateRange(1, 65535)]
    [int]$Port = 22
)

$ErrorActionPreference = 'Stop'
$ssh = (Get-Command ssh -CommandType Application | Select-Object -First 1).Source
$scp = (Get-Command scp -CommandType Application | Select-Object -First 1).Source
$sshOptions = @('-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3')
$python = (Get-Item -LiteralPath '.\.venv\Scripts\python.exe').FullName
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ('pchela-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage | Out-Null

$snapshot = @'
import json
import sqlite3
import sys
import time
from pathlib import Path

from shop.config import Config

config = Config.load()
stage = Path(sys.argv[1])
values = {
    'BOT_TOKEN': config.token,
    'ADMIN_IDS': ','.join(map(str, config.admins)),
    'ADMIN_URL': config.admin_url,
    'SUPPORT_URL': config.support_url,
    'REFERRAL_PERCENT': str(config.referral_percent),
    'SHOP_CURRENCY': config.currency,
    'CRYPTOBOT_TOKEN': config.cryptobot,
    'XROCKET_TOKEN': config.xrocket,
    'CRYPTOBOT_TESTNET': str(config.testnet).lower(),
}
backup_deadline = time.monotonic() + 60

def check_backup_progress(status, remaining, total):
    if time.monotonic() > backup_deadline:
        sys.exit('SQLite backup exceeded 60 seconds.')

with sqlite3.connect(Path(config.database).resolve().as_uri() + '?mode=ro', uri=True) as source:
    with sqlite3.connect(stage / 'shop.sqlite3') as target:
        source.backup(target, pages=128, progress=check_backup_progress)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            sys.exit('SQLite integrity check failed.')
        if target.execute('SELECT value FROM settings WHERE name = ?', ('currency',)).fetchone() != (config.currency,):
            sys.exit('SQLite currency differs from SHOP_CURRENCY.')
(stage / 'shop.env').write_text(
    ''.join(key + '=' + json.dumps(value, ensure_ascii=False) + chr(10) for key, value in values.items()),
    encoding='utf-8',
)
'@

try {
    Write-Host '[1/5] Creating and checking the SQLite backup.'
    & $python -c $snapshot $stage
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the SQLite backup and settings.' }

    $archive = Join-Path $stage 'release.zip'
    Write-Host '[2/5] Downloading and unpacking PCHELA from GitHub.'
    Invoke-WebRequest -UseBasicParsing 'https://github.com/ZiG777/PCHELA/archive/refs/heads/main.zip' -OutFile $archive -TimeoutSec 60
    Expand-Archive -LiteralPath $archive -DestinationPath $stage
    $installer = Join-Path $stage 'PCHELA-main/deploy/install-on-debian.sh'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw 'The downloaded release does not contain the Debian installer.'
    }

    Write-Host "[3/5] Opening SSH to ${Server}:$Port."
    $remoteStage = & $ssh @sshOptions -p $Port $Server 'umask 077; mktemp -d /tmp/pchela-import.XXXXXXXX'
    if ($LASTEXITCODE -ne 0 -or $remoteStage -notmatch '^/tmp/pchela-import\.[a-zA-Z0-9]{8}$') {
        throw 'Could not open a private staging directory on the server.'
    }

    Write-Host '[4/5] Uploading the code, SQLite backup and settings.'
    & $scp @sshOptions -P $Port $archive (Join-Path $stage 'shop.env') (Join-Path $stage 'shop.sqlite3') $installer "${Server}:$remoteStage/"
    if ($LASTEXITCODE -ne 0) { throw 'Could not transfer the release and SQLite backup.' }

    Write-Host '[5/5] Installing and starting PCHELA on Debian.'
    & $ssh @sshOptions -t -p $Port $Server "if [ `$(id -u) -eq 0 ]; then bash '$remoteStage/install-on-debian.sh' '$remoteStage'; else sudo -- bash '$remoteStage/install-on-debian.sh' '$remoteStage'; fi"
    if ($LASTEXITCODE -ne 0) { throw 'Debian installation failed. See the output above.' }
} finally {
    $stagedSettings = Join-Path $stage 'shop.env'
    if (Test-Path -LiteralPath $stagedSettings) { Remove-Item -LiteralPath $stagedSettings -Force }
    $backup = Join-Path $stage 'shop.sqlite3'
    if (Test-Path -LiteralPath $backup) { Write-Host "Local SQLite backup: $backup" }
}
