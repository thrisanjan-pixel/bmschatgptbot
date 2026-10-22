# Run on this PC

The browser-free replacement is now preferred. Start it with `deploy/run-api.sh` in WSL and stop it with `deploy/run-api.sh stop`. The dashboard remains http://localhost:8000. It uses `runtime/api.sqlite3` and `runtime/api-service.log`, automatically restarts failed services, and reuses the local Telegram credentials. No Chrome windows are required. It tracks new shows and category availability; individual seats/rows/maps are not included. The older commands below are retained only for the browser-based version.

Ubuntu/WSL and the Python/browser dependencies have been installed. The local launcher uses ordinary visible Chromium because its live read succeeded where headless access returned HTTP 403. It does not hide automation, reuse another browser's cookies, bypass challenges, or disable TLS verification. Visible worker browser windows are expected; leave them open while monitoring.

Start from PowerShell:

```powershell
wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/local-preview.sh'
```

Open http://localhost:8000 for the private dashboard. Connect Telegram through http://localhost:8000/setup, using a rotated token and the destination chat ID. Start the Telegram bot first. Submit only on this local page, never in a chat message. Successful setup sends a labelled test message and saves credentials under `/root/.config/bms-watch/telegram.json` with owner-only permissions, outside this repository. The monitor waits without sending until setup succeeds. The setup page does not display saved credentials.

The supervisor restarts either child if it exits. It uses `runtime/preview.sqlite3` for persistent baselines/outbox, and `runtime/supervisor.log` for current logs; these files are ignored by Git. The initial scan is silent. Existing good data is preserved when source pages fail identity validation, time out, or return unknown seat statuses. Such failures are retried and reported by the dashboard. Partial checks do not establish full city coverage.

Stop from PowerShell:

```powershell
wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/local-preview.sh' stop
```

Keep Windows awake and connected. This starts local processes, not a Windows boot/logon service. Rebooting, sleeping or shutting down the PC interrupts monitoring. Restart with the command above; the database remains. Do not run an independent Railway monitor against the same Telegram chat simultaneously.
