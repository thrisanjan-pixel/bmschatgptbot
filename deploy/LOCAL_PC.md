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

### Row-specific watching on this PC

My watch now includes Watch specific rows. Choose movie/cinema/date/category, select one exact showtime, and click Load seat rows. Select individual rows or Select all rows, then save paused or enable. Row mode replaces broad show/category alerts. Only newly available seats in the selected rows notify; the first layout is a silent baseline. Notify once pauses after delivery.

The PC launcher enables a dedicated row worker. It uses the installed Playwright Chromium and xvfb-run to read the site's accessibility seat table, without selecting/reserving seats or showing desktop browser windows. Only requested/selected shows are loaded. Selected enabled shows are checked about once a minute; source failures preserve the last observation and retry after five minutes. Rows not yet exposed by the source cannot be invented. The source may omit unavailable rows from initial choices.

The Railway image remains browser-free and row monitoring is disabled there. Do not claim that row mode is deployed or supported on Railway without installing and testing its browser/display dependencies.
