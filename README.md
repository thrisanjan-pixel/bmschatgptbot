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
| `BMS_BACKGROUND_LISTINGS` | `1` | `0` stops listing checks for movies no alert asks for while an alert has listings to check, so alert listings get every showtime request. |
| `BMS_NIGHT_HOURS` | empty | IST hours such as `1-7` (01:00 to 06:59) when the showtime and seat hosts run at half speed. Empty turns it off. |
| `BMS_SHOWDAY_WINDOW` | `10800` | Seconds before a show's booking closes when it counts as show day: its seat reads jump the queue. `0` turns it off. |
| `BMS_SHOWDAY_RECHECK` | `60` | Sold-out re-check interval inside the show-day window, never below `BMS_SEAT_GAP`. |
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

## Hot-standby failover

Two copies of the bot run at the same time. Both poll BookMyShow and both detect releases, but only the **active** one sends Telegram messages. A small Cloudflare Worker (the watchdog) pings both every minute. If the primary stops answering for longer than `GRACE_SECONDS`, the watchdog promotes the standby, and the standby starts sending within about a second.

Nothing here turns on by itself. With none of the new variables set, the bot behaves exactly as before: it is a primary, it is active, and the Docker image starts it without Litestream.

### Architecture

```
                      Cloudflare Worker "screenwatch-watchdog"
                      Durable Object, alarm() every 60 s
                      POST /heartbeat to both; POST /promote on failover
                         |                         |
               Bearer HEARTBEAT_SECRET    Bearer HEARTBEAT_SECRET
                         v                         v
   +-------------------------------+     +-------------------------------+
   | PRIMARY (Railway)             |     | STANDBY (Oracle VM)           |
   | BOT_ROLE=primary              |     | BOT_ROLE=standby              |
   | runtime role: active          |     | runtime role: standby         |
   | polls BMS at full rate        |     | polls BMS at 50% rate         |
   | outbox -> Telegram sender ON  |     | outbox -> sender OFF          |
   | /data/state.sqlite3           |     | /data/state.sqlite3           |
   +---------------+---------------+     +---------------+---------------+
                   | litestream replicate                | litestream replicate
                   v                                     v
        +-----------------------------------------------------------+
        | Cloudflare R2 bucket    primary/...        standby/...    |
        +-----------------------------------------------------------+
                   ^                                     |
                   |  standby: litestream restore primary/ every 5 min
                   +-------------------------------------+  (alert config sync)
                                                          and once more on /promote
                                                          (outbox merge)

   Both instances: Healthchecks.io ping after a successful BMS check (max 1/min).
   Watchdog and dead letters: ntfy.sh push to your phone.
```

### Role model

- `BOT_ROLE` is what an instance was deployed as: `primary` (default) or `standby`.
- The **runtime role** is `active` or `standby`. Only an active instance sends Telegram messages. A primary starts active, a standby starts standby.
- The runtime role is stored in the database (`meta.runtime_role`), so a restart keeps a promotion. The supervisor logs it on start: `Screenwatch role: BOT_ROLE=standby, runtime role=standby`.
- Polling runs in both roles. A standby keeps finding releases and queueing them in its outbox, so it is ready the moment it is promoted. While standby it uses `STANDBY_RATE_FACTOR` (default 0.5) of the normal BookMyShow request rate.
- A standby copies the primary's alerts (the `alerts` table and the alert switches) from the primary's R2 replica every `STANDBY_SYNC_SECONDS`. Alerts are created and edited on the primary's dashboard only.
- `/readyz`, `/heartbeat` and the dashboard's **Failover** panel show the role.

### Endpoints

All three take `Authorization: Bearer $HEARTBEAT_SECRET` (compared in constant time). An unset secret, or one shorter than 32 characters, refuses every call with 401.

| Endpoint | What it does |
| --- | --- |
| `POST /heartbeat` | Records the watchdog's ping and its view of both instances. Answers with role, last successful BMS check, outbox depth, last Telegram success, last failed send and dead-letter count. |
| `POST /promote` | Restores the primary's newest replica from R2 into a temp file, merges its undelivered outbox, drops stale rows, sets the role to active and so starts the sender. Answers 200 with the role. A second call while active changes nothing. A failed restore never blocks promotion. |
| `POST /demote` | Sets the role to standby and stops the sender (manual failback). Idempotent. Queued rows stay. |
| `GET /readyz` | Public. Now JSON: ready flag, role, outbox depth, last Telegram success, last failed send, last BMS success. Still 200 when ready, 503 when not. |

### Outbox guarantees

- **Detect, then write, then send.** A detection writes its outbox row in the same SQLite transaction as the snapshot or seat-state change (`api_watch.ingest`, `row_watch.ingest`). Telegram is called later, by the sender, outside any transaction. A crash before the commit loses both the state change and the message, so the next poll finds the release again. A crash after it keeps the message.
- **Crash mid-send.** The sender claims a row for 120 s before calling Telegram. If the process dies during the call, the row is sent again after the claim expires.
- **Dedupe key.** Every alert carries a stable tag in its text, `[SHOWID:ROWSET:RELEASE_TS]`, for example `[ET00506990-20261125-AMBH:F-7,8:1795412345]`. The key is stored with the row. Two rows for the same alert and the same `SHOWID:ROWSET` within 10 minutes count as one release. The sender never sends a row whose release has already gone out, and the promotion merge never adds one.
- **Ambiguous timeout.** When a send times out after the request left (a read timeout, not a refused connection), Telegram may have shown it. With `OUTBOX_AMBIGUOUS_POLICY=assume_sent` (default) the row is counted as delivered, so it can never arrive twice. `retry` sends it again instead, which may duplicate.
- **Bounded retries and dead letters.** After `OUTBOX_MAX_ATTEMPTS` failed sends (flood waits from HTTP 429 do not count), the row moves to `suppressed_outbox` and one ntfy alert goes out. The dashboard shows the dead-letter count.
- **Telemetry.** `last_telegram_success_at`, `outbox_depth` and `last_failed_send_at` are in `/readyz`, `/heartbeat` and the Failover panel.

### Failover runbook

What happens on its own:

1. The primary misses heartbeats for more than `GRACE_SECONDS` (default 180 s).
2. The watchdog calls `POST /promote` on the standby. It retries every minute until the standby reports active.
3. The standby restores `s3://$LITESTREAM_BUCKET/primary/`, merges the primary's undelivered alerts newer than `PROMOTE_FRESH_SECONDS` (default 900 s), archives its own older ones, and becomes active.
4. ntfy: **"Primary lost; standby promoted."** If promotion fails: **"Primary lost; promoting the standby FAILED."**
5. If both instances stop answering: **"Both instances down."** The watchdog never fails back on its own.

What you do:

1. Open the standby dashboard. Check the Failover panel: role **active**, outbox depth falling, a recent Telegram success.
2. Find out why the primary died (Railway logs, billing, volume).
3. Do not restart the primary until you are ready to fail back (below). A restarted primary comes back **active**, and the watchdog warns **"Both instances report active"** because alerts may then be sent twice.

### Failback

1. Fix the primary and let it start. It is active (its stored role never changed).
2. At once, demote the standby so only one instance sends:

   ```bash
   curl -fsS -X POST -H "Authorization: Bearer $HEARTBEAT_SECRET" https://STANDBY_HOST/demote
   ```

3. Check both `/readyz`: primary `"role": "active"`, standby `"role": "standby"`.
4. Alerts edited on the standby while it was active are not copied back. Re-create them on the primary, or restore a backup from the standby's dashboard.

To make the primary standby instead (keep the standby in charge), call `/demote` on the primary.

### How to test failover

Run these against a test pair, never while you need alerts.

1. Both up: `curl https://PRIMARY/readyz` shows `active`; `curl https://STANDBY/readyz` shows `standby`.
2. Heartbeat by hand:

   ```bash
   curl -fsS -X POST -H "Authorization: Bearer $HEARTBEAT_SECRET" https://STANDBY_HOST/heartbeat
   ```

3. Stop the primary (Railway: remove the domain or stop the service on a test project). Within `GRACE_SECONDS` + 60 s the ntfy push arrives and the standby `/readyz` shows `active`.
4. Call `/promote` again: the answer is `{"role": "active", "changed": false}`.
5. Start the primary, then fail back as above. The watchdog's both-active warning goes away after `/demote`.
6. Watchdog state: `curl -H "Authorization: Bearer $HEARTBEAT_SECRET" https://screenwatch-watchdog.YOUR.workers.dev/status`.

Automated tests: `python -m pytest tests/test_failover.py` (Python side) and `cd watchdog && npm test` (decision function).

### Failover environment variables

| Variable | Where | Default | Meaning |
| --- | --- | --- | --- |
| `BOT_ROLE` | bot | `primary` | `primary` starts active, `standby` starts standby. |
| `HEARTBEAT_SECRET` | bot + Worker | (unset) | Shared secret for `/heartbeat`, `/promote`, `/demote`. 32 characters or more, or the endpoints stay closed. |
| `STANDBY_RATE_FACTOR` | bot | `0.5` | Share of the normal BookMyShow rate a standby uses (0.1 to 1). Fewer requests per hour, longer gaps. Set `1` for the full rate. |
| `STANDBY_SYNC_SECONDS` | bot | `300` | How often a standby copies the primary's alerts from R2 (minimum 60). |
| `PROMOTE_FROM_PREFIX` | bot | `primary` | R2 prefix a promoted standby restores from. |
| `PROMOTE_FRESH_SECONDS` | bot | `900` | Undelivered alerts older than this are archived at promotion instead of sent. |
| `OUTBOX_MAX_ATTEMPTS` | bot | `20` | Failed sends before a message is dead-lettered (429 waits do not count). |
| `OUTBOX_AMBIGUOUS_POLICY` | bot | `assume_sent` | `assume_sent` or `retry` for a send that timed out after leaving. |
| `NTFY_TOPIC_URL` | bot | (unset) | `https://ntfy.sh/your-topic` for dead-letter and stale-data pushes. |
| `NTFY_TOKEN` | bot | (unset) | Bearer token if your ntfy topic needs one. |
| `HEALTHCHECKS_URL` | bot | (unset) | Healthchecks.io ping URL. Pinged after a successful BMS check, at most once a minute. Use a different check per instance. |
| `BMS_STALE_REFRESH_SECONDS` | bot | `1200` | Expected refresh of a show-day seat layout. Unchanged for `BMS_STALE_FACTOR` times this, it is flagged as suspect stale. `0` turns it off. |
| `BMS_STALE_FACTOR` | bot | `3` | Multiplier for the staleness check (minimum 2). |
| `LITESTREAM_BUCKET` | bot | (unset) | R2 bucket name. With the three below, the image runs the bot under Litestream. |
| `LITESTREAM_ENDPOINT` | bot | (unset) | `https://ACCOUNT_ID.r2.cloudflarestorage.com` |
| `LITESTREAM_ACCESS_KEY_ID` | bot | (unset) | R2 API token access key id. |
| `LITESTREAM_SECRET_ACCESS_KEY` | bot | (unset) | R2 API token secret. |
| `LITESTREAM_REPLICA_PATH` | bot | `$BOT_ROLE` | R2 prefix this instance writes to. |
| `PRIMARY_URL`, `STANDBY_URL` | Worker | placeholders | Base HTTPS URLs of the two instances. |
| `GRACE_SECONDS` | Worker | `180` | How long the primary may miss heartbeats before promotion. |
| `INTERVAL_SECONDS` | Worker | `60` | Heartbeat interval (minimum 30). |
| `NTFY_TOPIC`, `NTFY_URL` | Worker | (unset), `https://ntfy.sh` | Topic for watchdog pushes. |

### Manual setup

Do these steps in this order. None of them is automated, and the bot keeps working as a single primary until the last step.

**1. ntfy topic**

1. Install the ntfy app on your phone.
2. Pick a long random topic name, for example `screenwatch-` plus 20 random letters. Anyone who knows it can read and post, so keep it private.
3. In the app, subscribe to that topic on `ntfy.sh`.
4. Test: `curl -d "test" https://ntfy.sh/YOUR_TOPIC`.

**2. Healthchecks.io checks**

1. Sign up at healthchecks.io and create two checks: `screenwatch-primary` and `screenwatch-standby`.
2. Period 5 minutes, grace 5 minutes.
3. Add the ntfy integration (or email) under Integrations.
4. Copy each check's ping URL. It goes into `HEALTHCHECKS_URL` on the matching instance.

**3. Cloudflare R2 bucket and token**

1. Cloudflare dashboard, then R2, then Create bucket: `screenwatch-db`. Location automatic.
2. R2, then Manage R2 API Tokens, then Create API token. Permission **Object Read & Write**, limited to bucket `screenwatch-db`. No expiry, or a long one you will remember.
3. Copy the **Access Key ID**, the **Secret Access Key** and the S3 endpoint `https://ACCOUNT_ID.r2.cloudflarestorage.com`. The secret is shown once.

**4. Oracle Cloud VM (standby)**

1. Oracle Cloud Free Tier, then Compute, then Create instance. Image Ubuntu 22.04. Shape VM.Standard.A1.Flex (Ampere, 1 OCPU, 6 GB is enough). Add your SSH key.
2. Networking: in the VCN's security list, add ingress rules for TCP 80 and 443 from 0.0.0.0/0.
3. SSH in and open the same ports in the VM firewall (Oracle's Ubuntu image blocks them):

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   ```

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   ```

   ```bash
   sudo netfilter-persistent save
   ```

4. Install Docker and Caddy:

   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io caddy git
   ```

5. Point a DNS name at the VM's public IP (for example `standby.example.com`, or a free DuckDNS name).
6. Get the code and build the image (Docker picks the arm64 Litestream package):

   ```bash
   git clone https://github.com/thrisanjan-pixel/bmschatgptbot.git && cd bmschatgptbot && sudo docker build -t screenwatch .
   ```

7. Create `/opt/screenwatch/standby.env`, readable only by root (`sudo chmod 600`), with one `NAME=value` per line: `BOT_ROLE=standby`, `HEARTBEAT_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, the four `LITESTREAM_*` values, `HEALTHCHECKS_URL` (standby check), `NTFY_TOPIC_URL`, the same `BMS_*` speed settings as the primary, and `SCREENWATCH_PASSWORDLESS=0` with a `DASHBOARD_PASSWORD` of 16+ characters. The image defaults to a passwordless dashboard, which must not be public on the VM.
8. Run it:

   ```bash
   sudo docker run -d --name screenwatch --restart always -p 127.0.0.1:8000:8000 -v screenwatch-data:/data --env-file /opt/screenwatch/standby.env screenwatch
   ```

9. HTTPS: put `standby.example.com { reverse_proxy 127.0.0.1:8000 }` in `/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`.
10. Check: `curl https://standby.example.com/readyz` shows `"role": "standby"`.

Caution: memory from earlier trials says BookMyShow refused 50 to 81% of requests from the old Railway datacenter IP while the home PC got 0 of 30 refused. An Oracle VM is also a datacenter IP. Watch the standby's source requests for 403s in its first day before relying on it.

**5. Primary (Railway)**

Set on the Railway service: `BOT_ROLE=primary`, `HEARTBEAT_SECRET`, the four `LITESTREAM_*` values, `HEALTHCHECKS_URL` (primary check), `NTFY_TOPIC_URL`. The next deploy runs the bot under Litestream and starts writing `primary/` in R2. Check the logs for `Screenwatch role: BOT_ROLE=primary, runtime role=active` and Litestream's `replicating to` line.

**6. Watchdog Worker**

1. Install Node 20 or newer, then in `watchdog/`: `npm install`.
2. Log in: `npx wrangler login`.
3. Edit `watchdog/wrangler.toml`: `PRIMARY_URL` (the Railway HTTPS URL) and `STANDBY_URL` (`https://standby.example.com`).
4. Secrets:

   ```bash
   npx wrangler secret put HEARTBEAT_SECRET
   ```

   ```bash
   npx wrangler secret put NTFY_TOPIC
   ```

5. Deploy: `npx wrangler deploy`.
6. Arm it once (the Cron trigger also re-arms it within 5 minutes):

   ```bash
   curl -H "Authorization: Bearer $HEARTBEAT_SECRET" https://screenwatch-watchdog.YOUR.workers.dev/arm
   ```

7. After two minutes, both dashboards' Failover panels show a recent "Watchdog last ping".

## Files and validation

`city_watch.py` owns discovery and scheduling; `source.py` handles normal browser access; `parsers.py` validates public data; `seats.py` reads seat statuses; `store.py` maintains state/outbox; `telegram.py` delivers text and images; `dashboard.py` serves the local UI. `tests/` contains 21 behavioral tests. `deploy/` contains the external server installer and service definitions. The external deployment/reboot checks remain unrun until a server is available.

### Personal alert controls
Open My watch on the dashboard to select movies, cinemas, languages, formats, dates, seat categories and an IST time window. Alerts default to paused; enabling requires at least one movie. New shows are the default; category reopening is optional. A global limit allows at most one message per 10, 30 or 60 minutes. Excess changes are skipped, not replayed. Saving settings archives any pending old alerts. First observations remain silent. Choices persist in the local SQLite database.

