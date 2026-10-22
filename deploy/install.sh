#!/bin/sh
# Run on your own Debian 12/13 server, from the uploaded checkout.
set -eu
if [ "$(id -u)" != 0 ]; then
    echo 'Run with sudo on the target server.' >&2
    exit 1
fi
. /etc/os-release
if [ "$ID" != debian ]; then
    echo 'This installer targets Debian 12/13 with the native Chromium package.' >&2
    exit 1
fi
apt-get update
apt-get install -y python3 python3-venv chromium ca-certificates
if ! id bmswatch >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/bmschatgptbot --shell /usr/sbin/nologin bmswatch
fi
install -d -m 755 /opt/bmschatgptbot
install -d -o bmswatch -g bmswatch -m 700 /var/lib/bmschatgptbot
for file in city_watch.py source.py parsers.py seats.py store.py telegram.py dashboard.py requirements-browser.txt; do
    install -m 644 "$file" "/opt/bmschatgptbot/$file"
done
install -d /opt/bmschatgptbot/static
install -m 644 static/index.html /opt/bmschatgptbot/static/index.html
python3 -m venv /opt/bmschatgptbot/.venv
/opt/bmschatgptbot/.venv/bin/pip install --disable-pip-version-check -r /opt/bmschatgptbot/requirements-browser.txt
install -m 644 deploy/bms-watch.service deploy/bms-dashboard.service /etc/systemd/system/
if [ ! -f /etc/bms-watch.env ]; then
    install -m 600 /dev/null /etc/bms-watch.env
fi
systemctl daemon-reload
echo 'Installed. Enter TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID securely in /etc/bms-watch.env.'
echo 'The default CHROMIUM_PATH is /usr/bin/chromium (Debian native package).'
echo 'Then run: sudo systemctl enable --now bms-watch bms-dashboard'
