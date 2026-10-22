#!/bin/bash
# Notification-free local preview. Keep the PC awake while using it.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p runtime
root="$PWD"
python="${BMS_PYTHON:-$HOME/.local/share/bms-verify/venv/bin/python}"
export BMS_DB="$root/runtime/preview.sqlite3"
export BMS_HEADLESS="${BMS_HEADLESS:-0}"
export CHROMIUM_PATH
CHROMIUM_PATH="${CHROMIUM_PATH:-$(find "$HOME/.cache/ms-playwright" -path '*/chrome-linux64/chrome' -type f | sort -V | tail -n 1)}"
test -x "$CHROMIUM_PATH"
if [[ "${1:-start}" == stop ]]; then
    for name in supervisor monitor dashboard; do
        if [[ -f "runtime/$name.pid" ]]; then
            pid="$(cat "runtime/$name.pid")"
            if [[ "$pid" =~ ^[0-9]+$ ]] && [[ -r "/proc/$pid/cmdline" ]]; then
                command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
                if [[ "$command" == *"$BMS_DB"* ]]; then kill -TERM "$pid"; fi
            fi
        fi
    done
    echo 'Preview stop requested.'
    exit
fi
for name in supervisor monitor dashboard; do
    if [[ -f "runtime/$name.pid" ]] && kill -0 "$(cat "runtime/$name.pid")" 2>/dev/null; then
        echo "Preview $name already running. Stop it before starting again." >&2
        exit 1
    fi
done
"$python" - <<'PY'
import os,subprocess,sys,time
from pathlib import Path
for name,script,extra in [('supervisor','supervise.py',['--allow-local-setup'])]:
    with open(f'runtime/{name}.log','a') as log:
        child=subprocess.Popen([sys.executable,'-u',script,'--db',os.environ['BMS_DB'],*extra],
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        Path(f'runtime/{name}.pid').write_text(str(child.pid))
    time.sleep(2)
    if child.poll() is not None:raise SystemExit(f'{name} failed; inspect runtime/{name}.log')
PY
echo 'Local bot started: http://localhost:8000. Configure Telegram at /setup on this PC.'
