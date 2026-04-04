#!/bin/sh
set -eu

DATA_DIR="${CLIPROXY_DATA_DIR:-/data}"
CONFIG_PATH="${CLIPROXY_CONFIG_PATH:-/data/web_config.json}"
STATE_PATH="${CLIPROXY_STATE_FILE:-/data/CLIProxyAPI-cleaner-state.json}"
BACKUP_ROOT="${CLIPROXY_BACKUP_ROOT:-/data/backups/cliproxyapi-auth-cleaner}"
REPORT_ROOT="${CLIPROXY_REPORT_ROOT:-/data/reports/cliproxyapi-auth-cleaner}"
WEB_LOG_PATH="${CLIPROXY_WEB_LOG_PATH:-/data/logs/web.log}"
CLEANER_LOG_PATH="${CLIPROXY_CLEANER_LOG_PATH:-/data/logs/CLIProxyAPI-cleaner.log}"
mkdir -p "$DATA_DIR" "$(dirname "$CONFIG_PATH")" "$(dirname "$STATE_PATH")" "$BACKUP_ROOT" "$REPORT_ROOT" "$(dirname "$WEB_LOG_PATH")" "$(dirname "$CLEANER_LOG_PATH")"

if [ ! -f "$CONFIG_PATH" ]; then
  python3 - <<'PY'
import json, os
from pathlib import Path
src = Path('/app/web_config.example.json')
dst = Path(os.environ.get('CLIPROXY_CONFIG_PATH', '/data/web_config.json'))
cfg = json.loads(src.read_text(encoding='utf-8'))
cfg['listen_host'] = os.environ.get('CLIPROXY_LISTEN_HOST', '0.0.0.0')
cfg['cleaner_path'] = os.environ.get('CLIPROXY_CLEANER_PATH', '/app/CLIProxyAPI-cleaner.py')
cfg['state_file'] = os.environ.get('CLIPROXY_STATE_FILE', '/data/CLIProxyAPI-cleaner-state.json')
raw_hosts = os.environ.get('CLIPROXY_ALLOWED_HOSTS', '*')
cfg['allowed_hosts'] = [x.strip() for x in raw_hosts.split(',') if x.strip()] or ['*']
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'[docker] created default config at {dst}')
PY
fi

exec "$@"
