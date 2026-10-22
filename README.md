# Hyderabad booking watch

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

## Files and validation

`city_watch.py` owns discovery and scheduling; `source.py` handles normal browser access; `parsers.py` validates public data; `seats.py` reads seat statuses; `store.py` maintains state/outbox; `telegram.py` delivers text and images; `dashboard.py` serves the local UI. `tests/` contains 21 behavioral tests. `deploy/` contains the external server installer and service definitions. The external deployment/reboot checks remain unrun until a server is available.
