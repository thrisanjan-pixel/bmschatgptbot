#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p runtime
export BMS_DB="$PWD/runtime/api.sqlite3"
export BMS_ROWS_ENABLED=1
python="${BMS_PYTHON:-$HOME/.local/share/bms-verify/venv/bin/python}"
if [[ "${1:-start}" == stop ]]; then
    if [[ -f runtime/api-supervisor.pid ]]; then
        pid="$(cat runtime/api-supervisor.pid)"
        if [[ "$pid" =~ ^[0-9]+$ ]] && [[ -r "/proc/$pid/cmdline" ]]; then
            command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
            if [[ "$command" == *"$BMS_DB"* ]]; then kill -TERM "$pid"; fi
        fi
    fi
    echo 'Browser-free bot stop requested.'
    exit
fi
"$python" - <<'PY'
import os,subprocess,sys,time
from pathlib import Path
with open('runtime/api-service.log','a') as log:
    child=subprocess.Popen([sys.executable,'-u','supervise.py','--api','--db',os.environ['BMS_DB']],
        stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
Path('runtime/api-supervisor.pid').write_text(str(child.pid))
time.sleep(3)
if child.poll() is not None:raise SystemExit('Service did not start. Inspect runtime/api-service.log')
print('Screenwatch is running at http://localhost:8000. No Chrome windows are used.')
PY
