#!/bin/bash
# Local validation only. Does not configure Telegram or start a permanent monitor.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v python3 >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
    echo 'Install python3 and python3-venv before running this script.' >&2
    exit 1
fi
venv="${BMS_VERIFY_VENV:-$HOME/.local/share/bms-verify/venv}"
python3 -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check -r requirements-browser.txt
"$venv/bin/python" -m unittest discover -s tests -v
"$venv/bin/python" -m playwright install --with-deps chromium
export CHROMIUM_PATH
CHROMIUM_PATH="$("$venv/bin/python" -c $'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n    print(p.chromium.executable_path)')"
diagnostic="$(mktemp -d)"
echo "Diagnostic database: $diagnostic/state.sqlite3"
"$venv/bin/python" -u city_watch.py --db "$diagnostic/state.sqlite3" --dry-run --seconds 60
