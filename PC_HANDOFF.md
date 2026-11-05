# Continue this project on the user's Windows PC

This document transfers project context from a cloud Codex conversation. It is a handoff summary, not a full chat transcript. Read it, README.md and deploy/RAILWAY.md before continuing. The user wants the assistant to perform the work, with minimal repeated questions.

## Current state (5 Nov 2026, Claude Code session)

This section supersedes the older sections below where they conflict. Those sections are kept as history.

- **Hosting:** the old Railway account was removed. Everything now runs on the user's new Railway account: project f7f005f3-75dc-4261-8901-49fc54bd222f, service 18cc5ac2-77e9-4aac-b0d5-d33350495df3, environment production, volume mounted at /data. Public dashboard: https://bmschatgptbot-production.up.railway.app (password protected). Restart policy is ALWAYS.
- **Railway CLI:** `runtime/railway-cli/node_modules/@railway/cli/bin/railway.exe` (browserless login is done). The deploy folder is a copy of the Dockerfile's files plus requirements-cloud.txt, Dockerfile, railway.toml, static/screenwatch.html and deploy/railway-entrypoint.sh. Run `railway up --ci --detach` from that folder. A variable change needs `railway redeploy --yes` to take effect.
- **Credentials:** Telegram and dashboard values are set as Railway variables via a PowerShell 5.1 port of APPLY_CREDENTIALS.ps1. The port sets `[Console]::InputEncoding` to UTF-8 without BOM, because a BOM broke the Telegram token. Never print or paste the values.
- **Verified live:** all three active alerts read seats with HTTP 200, and Telegram delivered alerts with the seat image and the exact show link. One alert (Ramayana, 5 Nov showtime) is paused because that showtime is no longer listed.
- **Source limits:** request budgets live in http_source.py (services-in 30/hour with a 120 s gap (raised from 6/hour on 5 Nov at the user's request), in.bookmyshow.com 60/hour with a 60 s gap, CDN 6/hour). Deadlines carry across deployments through `BMS_SOURCE_NOT_BEFORE` (http_source.carry_over); the user had it deleted on 5 Nov. The dashboard seat panel shows a live "Next read in m:ss" countdown and a **Retry now** button (`POST /api/seat-retry`, `row_watch.clear_source_wait`) that clears saved seat waits; the hourly budget still applies and a new 429 starts a new wait. Do not try to evade BookMyShow or Cloudflare blocks: no TLS impersonation, proxies, IP rotation or header spoofing.
- **Code:** main is at 43f9243 on https://github.com/thrisanjan-pixel/bmschatgptbot. PR #1 (railway-fixes) is merged, and the remote branch `railway-fixes` was deleted on 5 Nov 2026. Recent fixes: worker resilience, Telegram fallbacks, dashboard wording and phone usability, deadline carry-over, and real Cinemas counts in API mode. The Cinemas trend fills in after about two 15-minute samples.
- **Tests on Windows:** `PYTHONPATH=. python runtime/run-checks.py` (needs the tzdata and cryptography packages; run-checks.py is gitignored).
- **GitHub access:** git push and fetch work. GitHub CLI 2.103.3 at `C:\Program Files\GitHub CLI\gh.exe` is signed in as thrisanjan-pixel (browser device login, keyring). The official github plugin fails with "Authorization header is badly formatted" because `GITHUB_PERSONAL_ACCESS_TOKEN` is not set; use `gh` instead.
- **5 Nov later work (main bd195ea, deployed):** a newly picked showtime's seats load first; the page waits for queued seat reads instead of giving up after 90 s; 7 backend alert fixes (no dropped multi-cinema alerts, no lock held during Telegram sends, no double photo/text send, late alerts labelled, edits keep queued alerts, health warnings scale with seat spacing); dashboard JS fixes and a UI polish pass. Seat reads returned HTTP 200 after Retry now.
- **Deploying:** `runtime/cloud-release` is stale. Stage the Dockerfile's file list plus `requirements-cloud.txt`, `Dockerfile`, `railway.toml`, `static/screenwatch.html` and `deploy/railway-entrypoint.sh` in a scratch folder and run `railway up --ci --detach -p <project> -s <service> -e production` there. `railway ssh` needs an SSH key on this PC, which does not exist; the safety check blocks creating one and blocks remote DB writes.
- **Git identity:** none is configured on this PC; commit with `git -c user.name=Thrisu -c user.email=thrisanjan@gmail.com`.
- **Open items:** (1) The live dashboard has NO password: the Dockerfile sets `SCREENWATCH_PASSWORDLESS=1`. Ask the user before changing it. (2) Branch `claude/railway-config-ad096c` holds `.railway/railway.ts` replacing `railway.toml` (deprecated, works until 2026-12-01); it is unverified and not merged. (3) `cloud_row_reader.read` sends a Chrome User-Agent, which conflicts with the no-header-spoofing rule; the user has not decided. (4) Check that the Cinemas trend replaced "Collecting history".

## Goal and decisions already made

- Build a BookMyShow tracking bot like showting.in/dashboard for **ALL Hyderabad movies and cinemas**, with automatic discovery as listings change. The final ALL instruction supersedes an earlier shortlist discussion; do not ask for cinemas/movies again.
- Deliver Telegram notifications for newly added shows, including cinema, count, film, language/format, date, times and a booking link. Detect newly available individual seats/rows and attach a current native map image.
- Public source statuses do not reliably separate administratively blocked, sold or held seats. Say **Seats now available**, not a fabricated administrative unblock claim.
- The user wants instant alerts and 24/7 operation, but also explicitly requires **no spending**. Do not activate paid resources or assume trial credits cover ongoing operation. Polling cannot promise instant all-city detection; a source push feed is not connected.
- User chose Railway and reports being logged into Railway in their own browser. That does not authenticate a CLI/session on this PC. Establish supported account access locally; never ask for tokens/passwords in chat. Check actual account credits and limits before any deployment.
- GitHub destination: https://github.com/thrisanjan-pixel/bmschatgptbot (intended branch main). Source upload and Railway deployment are authorized within the no-spending constraint. Never force-push over existing remote work.

## What exists

The application has five threaded browser workers: catalogue, movie details, showtime/cinema discovery, venue/date pages, and seat maps. It follows native source URLs and all source-offered enabled future dates and language/format variants. Queue state and snapshots use SQLite WAL with a durable Telegram delivery outbox and per-database lock. Initial observations are silent; subsequent new shows/releases notify. Telegram delivery is at least once, so a crash after acceptance but before local acknowledgement can duplicate a message.

Modules: city_watch.py (scheduler), source.py (normal Chromium/TLS), parsers.py (native data validation), seats.py (read-only accessibility category/row filters), store.py (state/outbox), telegram.py (text/images), dashboard.py + static/index.html (private dashboard). supervise.py restarts local worker/dashboard children. Requirements pin Playwright 1.62.0. Tests are in tests/test_tracker.py. Dockerfile/railway.toml configure a single background worker; deploy/ also has Debian systemd assets.

## Validation already completed, with limits

- 21 rebuilt behavioral tests passed. They cover identity/cutoff validation, automatic dates/variants, silent baselines, new shows, seat releases in partly available rows, repeated release transitions, delivery retry, restart persistence and failed-source preservation.
- Normal browser access successfully loaded live catalogue, movie, venue and seat pages. One screening had 110 available seats; its native map preview was delivered to Telegram and explicitly labelled as a setup preview.
- Dashboard functional check: HTTP 200, 4 cards, 5 coverage rows, 52 movie entries, no JS errors or mobile horizontal overflow.
- Development supervisor restarted a stopped monitor and retained snapshots, queued tasks and cooldown.
- Last captured initial scan: 52 movie pages checked; 70 discovered cinemas, 808 future screenings; 53 cinema/date pages and 25 seat jobs checked. This was **partial inventory**, not complete city coverage. These are historical counts, not current status.
- An ARTH/20261022 cinema response lacked matching native data; the app retained prior state and counted an error. Do not replace failures with empty success.
- Source later returned 503. The cloud's outbound proxy then also failed for GitHub, Railway API, Docker Hub and npm, with envoy transport errors referencing cloudflare_https_tunnel. Do not attribute all failures to BookMyShow or the user's browser. Test connectivity anew from this PC before diagnosing source restrictions.
- Docker build stopped fetching python:3.12-slim-bookworm with registry HTTP 503. TOML and entrypoint shell syntax passed, but **the image/container and Railway deployment have not been validated**.
- No external deployment, actual future opening/release event verification, or 24/7 uptime proof exists yet. Trial/billing verification remains pending.

## Local source/history and transfer contents

The source was committed in the cloud as 67ccbf9ac54be0e8ea736317a60973c614aa942b (root commit on work). Actual `git push origin HEAD:main` failed with proxy HTTP 503, so no remote upload is confirmed. The source ZIP contains these files at its root and a transfer/bms-source.bundle preserving that commit. PC_HANDOFF.md itself was added after that commit. If using the bundle, restore history into a separate empty directory, then copy this handoff alongside it; do not clone over extracted files or discard edits. Alternatively initialize this extracted directory, inspect the remote and commit/upload source normally without force.

No tokens, chat IDs, .env files, runtime database, logs, cloud proxy credentials or certificate stores are included. That omission is intentional. A fresh local database starts with a new silent baseline. Do not commit any runtime database or secrets when publishing this source.

Telegram worked with secure cloud bindings, but those values do not transfer. The original conversation contained an exposed bot token. Ensure it has been rotated using BotFather if not already done, and configure the replacement and chat ID through local secure environment variables or Railway service variables. Never copy cloud proxy placeholder credentials to Railway. Do not ask the user to paste secrets into chat.

## Windows PC startup

The current application is Linux-oriented: city_watch.py imports fcntl; Chromium defaults to /usr/bin/chromium. **Do not claim native Windows Python runs it unchanged.** Use WSL2/Linux or the Linux Docker image, or implement and verify a deliberate Windows port. The screenshot shows Codex's “Finish Windows setup to continue” banner; complete that setup first and inspect the actual local execution environment.

Under WSL/Linux, install Python 3.12+, Chromium and the pinned requirements. Set BMS_DB to a writable persistent local path instead of assuming the old cloud /workspace path exists. Configure credentials securely. For tests: `python -m unittest discover -s tests -v`. For continuous monitoring: `python -u city_watch.py`; run `python dashboard.py` separately for the private loopback UI. Inspect progress and actual source errors. Avoid simultaneous independent local/cloud/Railway monitors posting duplicate notifications to the same chat.

If using Docker Desktop, build the supplied Dockerfile in Linux-container mode, mount a persistent volume at /data, and supply credentials using a secure environment file outside the repository. The container runs the worker only; it does not expose the private dashboard. Verify the build and functional reads before claiming it works.

The old environment's private proxy CA was explicitly approved only for that environment. Do not import it on the PC or Railway or disable TLS verification. Normal public TLS should work without it.

## Next work to perform

1. Inspect the local environment and network and preserve all extracted files. Set up Linux/WSL or Docker as appropriate; do not perform paid operations.
2. Run the tests and real functional checks. Build/smoke-test the Railway image now that a different network is available. Diagnose failures rather than repeatedly retrying unchanged requests.
3. Inspect the actual GitHub remote before uploading. The user authorized uploading the prepared source; avoid destructive pushes and never include credentials/runtime data. GitHub authentication on the PC is separate from the cloud's injected proxy auth.
4. Establish Railway access through its supported secure login flow. Inspect account trial/free allowance, volume availability and continuous-worker limits. No spending is authorized; if required resources cost money, stop that deployment step and state the exact blocker.
5. If the account can support deployment without spending, follow deploy/RAILWAY.md: one replica, no sleeping, /data persistent volume, BMS_DB=/data/state.sqlite3, secure Telegram variables, outbound HTTPS. Do not expose the private dashboard publicly without authentication/HTTPS.
6. Verify fresh heartbeat plus increasing successful coverage, labelled Telegram delivery, volume persistence through redeployment and an actual later source change. Source cooldown is not successful coverage. State polling limits accurately.

Keep working independently on authorized reversible tasks; ask only for access or constraints that are actually missing. Do not tell the user the system is fully instant/24/7 until evidence supports those claims.
