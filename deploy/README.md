# Always-on deployment

This development cloud session is not an always-on host. No server account is connected, so production deployment has not happened. A systemd unit does not provide hosting by itself.

Use a Debian 12/13 VPS, preferably 2 CPUs and 4 GB RAM for the five browser workers. Debian is used here because its native Chromium package fits the supplied service configuration. Upload this checkout using SSH. Do not send passwords, private keys or Telegram tokens in chat.

1. From the uploaded checkout run `sudo sh deploy/install.sh`. The installer requires package-network access and root privileges on **your server**.
2. Securely edit `/etc/bms-watch.env` on the server (root-owned, mode 600): set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `CHROMIUM_PATH` to that server's Chromium executable. Use the real credentials from your Telegram bot configuration, not the cloud proxy's placeholder values.
3. Run `sudo systemctl enable --now bms-watch bms-dashboard`. Both restart on failure and start after a reboot. The dashboard binds only to loopback. Use an SSH tunnel to view it; public hosting requires separate authentication and HTTPS.
4. Check `sudo journalctl -u bms-watch -n 50 --no-pager` and `curl --fail http://127.0.0.1:8000/api/status`. Confirm current heartbeat, increasing successful checks, no source errors and eventual initial inventory completion. An active process alone does not prove coverage.
5. Verify a labelled setup Telegram message, then wait for an actual source change to verify a production event. Never label a replay or test as a new booking opening.
6. Exercise `sudo systemctl restart bms-watch` and a server reboot. Confirm heartbeat returns and the database persists. Take SQLite backups with Python's SQLite backup API, not a copy of a live WAL database.

Keep `/var/lib/bmschatgptbot` backed up. Losing it resets baselines and queued alerts. Delivery is at least once: a crash after Telegram accepts a message but before its database acknowledgement can repeat that message. BookMyShow can throttle requests or change its public layout; the monitor backs off and retains observations on failures.

The installer and systemd files are deployment assets; they have not been installed on an external VPS in this session. The application polls; true instant citywide alerts require an authorized push or streaming data feed from the source.
