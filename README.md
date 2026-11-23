# Hyderabad booking watch

## Current PC version: Screenwatch

The preferred local version now uses direct HTTP reads, with **no Chrome windows**. Open http://localhost:8000 for the redesigned dashboard. It automatically discovers Hyderabad movies through the public catalogue fallback used by BookMyShow's website, then checks source-offered dates and show/category availability. Catalogue cache updates may lag the live site. Individual seats, rows and native map images belong to the older browser implementation and are not included in this simpler version.

Start from PowerShell:

```powershell
wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/run-api.sh'
```

Stop with the same command followed by `stop`. The supervisor restarts the API monitor and dashboard after a failure. State/outbox are in ignored `runtime/api.sqlite3`, logs in `runtime/api-service.log`. The first observations are silent. Configure Telegram only at http://localhost:8000/setup; saved owner-only credentials stay outside this repository. Keep the PC awake and connected. No boot/logon service has been installed.

Validated on this PC: 31 tests passed; live catalogue and showtime reads succeeded; a labelled Telegram test was accepted; worker restart retained state and resumed scanning. This is polling and does not prove complete city coverage or instant detection. The API response parser was informed by [aviiciii/bms-ticket-notifier](https://github.com/aviiciii/bms-ticket-notifier); its MIT notice and reference source are retained under `third_party/bms_ticket_notifier/`.

The Railway Dockerfile now runs the API worker and password-protected mobile dashboard. See deploy/RAILWAY.md for secure setup and current deployment blockers. The image has not yet been built on Railway. The sections below describe the historical browser implementation.

For Railway deployment, use the root Dockerfile and railway.toml with a persistent volume. See [Railway instructions](deploy/RAILWAY.md). Railway's ongoing usage may cost money; these files do not activate a deployment.

Telegram notifications and a private dashboard for automatically discovered Hyderabad BookMyShow movies, cinemas, language/format variants, source-offered dates and screening seat maps. New-show notifications include the cinema, movie, language, format, date, times and booking link. Seat notifications include rows, seat numbers and a current native map image.

## What works and what remains

The rebuilt application has successfully read live catalogue, movie, cinema and seat-map pages and delivered a labelled native map preview to the configured Telegram chat. The first complete inventory is a silent baseline; subsequent observed changes generate alerts. Unit tests cover new-show transitions, seat releases within partly available rows, repeat releases, durable delivery retries, restart persistence and source identity checks. A real future opening or release cannot be manufactured to validate detection against BookMyShow; controlled transitions are tested separately from live read/delivery checks.

This is polling, not a guaranteed instant feed. Cinema and seat tasks become eligible again 30 seconds after their last check; movie/catalogue/discovery tasks use 300 seconds. Each queue is processed fairly by its own browser worker, with at least one second between navigations across workers. Large citywide queues can take many minutes or hours for a full seat cycle. The dashboard reports queued versus successfully checked pages and oldest observation ages. Never describe those intervals as a citywide 30-second guarantee. True instant citywide alerts require an authorized source push feed that is not provided here.

The cloud development instance is temporary. Live processes do not survive instance shutdown or snapshot restoration. No external VPS is connected, and 24/7 production operation has **not** been activated. See [deployment instructions](deploy/README.md) for systemd services with automatic restart and boot startup. Public web hosting, sign-in and multi-user subscriptions are not implemented.

## Run in this environment

Use the existing checkout, not a worktree. Python 3.12+, Chromium and Playwright 1.62 are required. Keep credentials in secure environment settings: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never put them in source, command arguments or logs. Cloud proxy-backed secret placeholders work only through the supplied proxy; they are not exportable server credentials.

```sh
cd /workspace/bmschatgptbot
python -m venv --system-site-packages /workspace/booking-tracker/venv
/workspace/booking-tracker/venv/bin/python -m pip install --disable-pip-version-check -r requirements-browser.txt
/workspace/booking-tracker/venv/bin/python -m unittest discover -s tests -v
/workspace/booking-tracker/venv/bin/python -u city_watch.py
```

Run `python dashboard.py` in another terminal. It binds to loopback port 8000; `/api/status` returns private status without credentials. Local requests can validate it internally. The onboarding UI does not provide localhost previews.

Alternatively, run `python -u supervise.py` to launch both services and restart either after an unexpected exit. Stop preexisting instances first; do not run this alongside the commands above. The development supervisor only operates while this machine is active. Production should use the systemd units instead.

The database defaults to `/workspace/booking-tracker/state.sqlite3`; override with `BMS_DB` or `--db`. It stores discovery, source cooldown, task scheduling, snapshots and the delivery outbox. A file lock prevents two monitors using the same database. The previous `movies` table is imported without discarding legacy data. Take consistent backups with SQLite's backup API. A pre-rebuild backup lives outside the checkout in this development environment.

For a bounded live diagnostic use `python city_watch.py --db /tmp/diagnostic.sqlite3 --dry-run --seconds 60`. Dry-run suppresses notifications but **does write that database**; never use the real database for a disposable diagnostic. SIGTERM/SIGINT stop workers cleanly; a service manager can restart the process. Telegram's at-least-once delivery can repeat a message after a crash between remote acceptance and local acknowledgement.

## Source correctness

The reader uses normal public pages and native initial-state data, validates Hyderabad, event, venue, date and cutoff identities, and rejects unknown seat statuses. It follows genuine cinema URLs and all enabled future dates exposed by the source. No guessed cinema slug or fixed film shortlist limits ALL mode. Fully unavailable sessions can seed an empty seat baseline; coarse summaries never overwrite an actual seat-map snapshot. Expired or removed sessions leave active queues.

Seat-map reading opens the quantity/map screen and filters accessibility categories/rows. It never selects individual seats, reserves tickets or enters payment. Public statuses cannot reliably identify sold versus held versus administratively blocked seats, so notifications truthfully say **Seats now available**.

HTTP 403, 429 and 5xx responses pause source requests with persisted cooldown and Retry-After handling. Errors preserve prior good observations. Chromium uses normal TLS verification. The user approved access to this environment's supplied proxy CA in its existing certificate store; restricted-shell certificate-store access may require the supported escalation tool. Do not disable certificate verification, install unrelated roots or bypass source challenges.

## BookMyShow request policy (rate limits and cooldowns)

The bot stays inside BookMyShow's limits and slows down when told to. It never uses proxies, IP rotation or any other way around BookMyShow or Cloudflare.

Every request to BookMyShow goes through `http_source.open_request`, which keeps a per-host budget in SQLite (`source_requests`, `source_hosts`, `source_health`). That state survives restarts and redeploys. A restarted worker cannot send a burst, and a cooldown started before the restart is still in force after it.

**Spacing and caps.** `services-in.bookmyshow.com` (seat layouts) and `in.bookmyshow.com` (showtimes) each allow at most 60 requests in any rolling hour. Consecutive requests to one host are at least 60 seconds apart. Alert checks add 0 to `BMS_REQUEST_JITTER` seconds of random extra spacing, so requests never land on a fixed beat. `in-cdn.bmscdn.com` (catalogue) allows 6 per hour, 600 seconds apart. Manual "Load seat rows" and seat-metadata lookups have their own small hourly budgets, but they still respect the 60-second host spacing. The request timeout (25 s) is shorter than that spacing, so at most one request per host is in flight at a time. There is no burst or rate-test mode.

**HTTP 429.** A 429 rests the whole host (every lane, including manual reads) until its `Retry-After` time, with a minimum of 5 minutes. The deadline is stored in `source_hosts.not_before` (and `meta.seat_cooldown_until` for seat layouts), so it survives restarts. "Retry now" on the dashboard never clears a 429 deadline.

**HTTP 403.** A 403 served by Cloudflare (`Server: cloudflare`, a `CF-Ray` or a `CF-Mitigated` header) is recorded as `cloudflare_block`; any other 403 is `forbidden`. Either one rests the host with exponential backoff: `BMS_403_BACKOFF_BASE` seconds after the first, then double after each repeat, up to `BMS_BACKOFF_MAX`. One successful response resets the streak.

**Circuit breaker.** Network failures and 5xx replies count toward a streak. After `BMS_BREAKER_THRESHOLD` failures in a row the host rests for `BMS_BREAKER_COOLDOWN` seconds, doubling on each further failure up to `BMS_BACKOFF_MAX`.

**Operator alert.** When a host is rate-limited, blocked (`BMS_BLOCK_ALERT_AFTER` 403s in a row) or the breaker opens, one Telegram message goes out for that episode. It names the host, the reason and roughly how long the bot will pause.

**Sold out is not an error.** BookMyShow answers a seat read for a sold-out show with HTTP 200 and `intException` 3306 (`... sold out ... (#159)`). The bot stores that show as **Sold out**, records the read as successful, and forgets which seats were open, so any released seat alerts again. The show is then re-read only every `BMS_SOLD_OUT_RECHECK` seconds. In between, the bot uses the showtime listing it already fetches every minute as a free probe. As soon as the listing reports any category with seats, the full layout is read at once. The probe only gates shows the seat source itself confirmed sold out. A show with open seats is never skipped because of the listing, since the listing can lag behind the layout.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `BMS_SEAT_GAP` | `60` | Seconds between seat-layout requests; the hourly seat limit becomes 3600 divided by it. Never below 20: a one-hour test at 20 s got no 429, while 10 s drew a 21-hour block. |
| `BMS_REQUEST_JITTER` | `15` | Maximum random extra seconds between alert checks to one host. `0` turns jitter off. |
| `BMS_403_BACKOFF_BASE` | `180` | First rest after a 403, in seconds. It doubles on each repeat. |
| `BMS_BREAKER_THRESHOLD` | `3` | Consecutive network or 5xx failures that open the circuit breaker. |
| `BMS_BREAKER_COOLDOWN` | `600` | First breaker rest, in seconds. It doubles on each further failure. |
| `BMS_BACKOFF_MAX` | `21600` | Longest 403 or breaker rest (6 hours). A 429 `Retry-After` is never shortened. |
| `BMS_BLOCK_ALERT_AFTER` | `2` | Consecutive 403s before the operator alert. A 429 alerts at once. |
| `BMS_SOLD_OUT_RECHECK` | `900` | Seconds between seat reads of a confirmed sold-out show (minimum 120). |
| `BMS_LISTING_PROBE` | `1` | `0` turns off the listing probe; sold-out shows are then re-read only on the backstop. |
| `BMS_SEAT_SOURCE` | `server` | `worker` hands seat-layout reads to the home seat worker. |
| `BMS_WORKER_TOKEN` | (unset) | Shared secret for the worker API, 32 characters or more. Set the same value on the server and the PC. |
| `BMS_WORKER_STALE` | `180` | Seconds without contact before the worker counts as offline. |
| `BMS_WORKER_FALLBACK` | `none` | `server` lets the server read seat layouts itself while the worker is offline. |

State is kept in SQLite on the Railway volume. Redis was not added: one container owns the database, and SQLite already persists every budget, cooldown and streak.

### Split setup: home seat worker

Seat layouts can be read from a home connection while the server keeps the listings, alerts, dashboard and Telegram.

1. Generate a long random token (for example `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
2. On Railway, set `BMS_SEAT_SOURCE=worker` and `BMS_WORKER_TOKEN=<token>`. Optionally set `BMS_WORKER_FALLBACK=server`.
3. On the home PC, install `requirements-cloud.txt`, then run:

   ```
   set SCREENWATCH_URL=https://<your-app>.up.railway.app
   set BMS_WORKER_TOKEN=<token>
   python home_worker.py
   ```

The worker calls `POST /api/worker/claim` (bearer token) for the next due seat read. It reads the layout from the PC through the same request budget, backoff and breaker, using its own `home_worker.sqlite3`. Then it posts the layout, the sold-out answer or the error to `POST /api/worker/result`. A handed-out read is leased for 120 seconds. If the worker dies mid-read, the read is handed out again.

If the worker stops calling for `BMS_WORKER_STALE` seconds, the server sends one Telegram message and the dashboard status shows `seat_worker_offline`. Showtime alerts keep working. Seat-row alerts wait for the worker, unless `BMS_WORKER_FALLBACK=server` is set; then the server reads layouts itself until the worker returns.

## Files and validation

`city_watch.py` owns discovery and scheduling; `source.py` handles normal browser access; `parsers.py` validates public data; `seats.py` reads seat statuses; `store.py` maintains state/outbox; `telegram.py` delivers text and images; `dashboard.py` serves the local UI. `tests/` contains 21 behavioral tests. `deploy/` contains the external server installer and service definitions. The external deployment/reboot checks remain unrun until a server is available.

### Personal alert controls
Open My watch on the dashboard to select movies, cinemas, languages, formats, dates, seat categories and an IST time window. Alerts default to paused; enabling requires at least one movie. New shows are the default; category reopening is optional. A global limit allows at most one message per 10, 30 or 60 minutes. Excess changes are skipped, not replayed. Saving settings archives any pending old alerts. First observations remain silent. Choices persist in the local SQLite database.

