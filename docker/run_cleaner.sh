#!/bin/sh
set -eu

CONFIG_PATH="${CLIPROXY_CONFIG_PATH:-/data/web_config.json}"
while true; do
  if [ -f "$CONFIG_PATH" ]; then
    if python3 - "$CONFIG_PATH" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    cfg = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(2)
base_url = str(cfg.get('base_url', '')).strip()
management_key = str(cfg.get('management_key', '')).strip()
cleaner_path = str(cfg.get('cleaner_path', '')).strip()
if not cleaner_path:
    raise SystemExit(2)
if (not base_url) or ('example.com' in base_url) or (not management_key) or (management_key == 'replace-me'):
    raise SystemExit(2)
raise SystemExit(0)
PY
    then
      exec /usr/local/bin/python /app/run_cleaner.py
    fi
  fi
  echo '[docker] waiting for a valid web_config.json (set real base_url / management_key / cleaner_path first)...' >&2
  sleep 15
done
