# Continue this project on the user's Windows PC

This document transfers project context from a cloud Codex conversation. It is a handoff summary, not a full chat transcript. Read it, README.md and deploy/RAILWAY.md before continuing. The user wants the assistant to perform the work, with minimal repeated questions.

## Update (24 Nov 2026, ~23:00 IST, Claude Code)
- Live is now Railway deployment `b614afa4-a043-47e9-b075-19e442cf52da` = branch `feat/listing-interval-20` commit `26a58f5` (PR #18, open), on top of main `c61c462` (PR #17 merged).
- Change: the Activity "Showtime check interval" now also offers 20 seconds (`LISTING_INTERVALS = (20, 30, 60, 120)` in `http_source.py`). Default stays 60 s. 20 s is 180 showtime requests per hour, three times the verified-safe 60 s; use it only for short bursts.
- After deploy: healthz and readyz 200; the 20-second option is on the live page.

## Update (24 Nov 2026, ~22:45 IST, Claude Code)
- Live is now Railway deployment `3d9fbf5f-9428-49ff-afe9-6b7301f9aab3` = branch `fix/drop-paused-seat-reads` (PR #17), built on main `2e668cc` (PR #16 merged; main had matched live `5e1ad237`).
- Fix: pausing, editing or finishing an alert used to leave its queued seat reads in the queue, so each ran once more (seen with the paused AMB Gachibowli 17 Dec alert). `row_watch.schedule_active` now stores the showtimes it queued for alerts in meta `seat_alert_targets` and clears the queued read (`due=0`, only when `manual=0`) of any showtime no alert watches any more. "Load seat rows" presses are not touched.
- After deploy: healthz and readyz 200, BookMyShow requests 200. No alert is active right now (Ameerpet alert completed, AMB and ALUC 17 Dec paused), so the check queue is empty apart from the running 175-check inventory refresh.
- Still open: the showtime interval is 30 s (120/h); the verified-safe rate is 60 s. Set it back in Activity.

## Latest state (24 Nov 2026, 21:55 IST, Claude Code) — read this first

This section supersedes everything below where they conflict. Keep it current after every change to the bot.

**Production**
- Railway account thrisanjan, project `0ac5e4f5-448d-4d9c-a50f-437a048f743c`, service `0a52398c-1b0c-469c-bcfd-4a9f72ff6358`, environment `eba299a0-0319-4517-9695-39813013bb3b`. URL https://bmschatgptbot-production.up.railway.app. The service has no GitHub source; every deploy is a CLI `railway up` from a clean tree.
- Live deployment `5e1ad237` (24 Nov, about 21:50 IST) = branch `feat/restore-redesign` head (pushed; not merged into `main` yet). Before that, `1344c4e4` = commit `4a2cc1f` (pushed to GitHub; not merged into `main` yet). It is `main` `6af2564` plus two things: (a) ChatGPT/Codex's changes that went live at 21:18 IST as deployment `9d77c80c` from the folder `runtime/inventory-button-release` (inventory refresh button, 30/60/120 s showtime interval in Activity, `watch_preferences.movie_key` so "AOT - Aakasamlo Oka Tara" matches "Aakasamlo Oka Tara", alert restart fixes), and (b) the black glass dashboard redesign from `codex/seat-helper-checkout` (`1efa62e` to `43b1537`, 13 to 18 Nov) with the Geist fonts. Deploy from a clean worktree of this branch (or `main` once it is merged), never from the PC checkout `codex/seat-helper-checkout`, which has many uncommitted, unreviewed changes.
- Rollback point: tag `safe-2026-11-24` (main `1e66ddc`, deployment `06a6ebb7`).
- Railway variables: `BMS_SEAT_GAP=20` (lowered from 30 on 24 Nov; the code floor is 20; 15 s and 10 s got HTTP 429 with Retry-After 86400), `BMS_REQUEST_JITTER=0`, `BMS_SOLD_OUT_RECHECK=180`, `BMS_BACKGROUND_LISTINGS=0`, `BMS_NIGHT_HOURS=1-7`. Showtime listings stay at 60 per hour.
- Health on 24 Nov: no BookMyShow 403 or 429 since the 04:56 IST deploy. The last refusals (403 listing, 429 seat) were at 02:18 and 02:26 IST, before it.

**Changes on 24 Nov 2026**
1. PR #13 (`8e08e3e`): Open on PC has no 3-hour limit. An alert stores `open_on_pc_since` (arm time) instead of `open_on_pc_until`. The arm stays until the alert opens on the PC or is turned off. Old arms are converted on read. The extension's `isArmedEvent` accepts both forms.
2. PR #14 (`7ef8e87`): a never-checked alert listing used to be ranked "300 s overdue", recomputed every turn, so it never aged. Six routine listings starved a new alert forever. `api_watch.listing_order` now stores that place once.
3. The AOT alert (`a81136a0`) was set to the movie "AOT", which is BookMyShow `ET00526277`, the Tamil 2D version with no shows. It is now set to "AOT - Aakasamlo Oka Tara", the title BookMyShow gives the shows. The Telugu Dolby Cinema version is `ET00526345`; its show is ALLU Kokapet, 25 Nov, 7:50 PM. Its seat types are PLATINUM, DIRECTOR CHOICE, GOLD and "GOLD."; the alert still had "3D GOLD", and the owner was told to change it.
4. Why the UI changed back: the redesign was only ever on `codex/seat-helper-checkout`, never on `main`. The new Railway account was first deployed from `main` (`1e66ddc`), so it showed the old dashboard. Commit `4a2cc1f` restores the redesign on top of the live backend. HTML conflicts were resolved by keeping the redesign layout and adding Codex's elements (`inventory-refresh`, `filter-matches`, `listing-cadence`, `listing-cadence-summary`); the alert health line and the status banner use Codex's version (verification-failed states). The redesign's Telegram send list and blind-alert label are in the page but stay inactive, because their backend (`/api/deliveries`, `alert_health.py`) is not in this build. Before any new deploy, start from `4a2cc1f`, or the dashboard will revert again.
5. Commit `d94767b` brings back the four panels from the last old-account build (`runtime/railway-verification-live-test`, deployed by Codex 24 Nov 01:10 IST): "Refresh tracked listings" in the check queue (`refresh_listings` action), the Warnings panel (`alert_health.py`: a per-alert "BMS checks blocked for N min" Telegram warning plus recovery, minutes saved via `POST /api/blind-settings`, and it replaces the duplicate listing-staleness warning for listing alerts), the next 7 days in the date picker (`calendar_dates`), and "Listing version" labels plus clearer seat-retry text. The refresh buttons now show progress ("Refreshing · 12/175") instead of only greying out. Deployment `ed2462a3` crashed every worker with ModuleNotFoundError for about 3 minutes because the Dockerfile copies files by name and missed `alert_health.py`; the next commit adds it and `5e1ad237` is healthy. When you add a Python module, add it to the Dockerfile COPY line.

**Known problems, not fixed yet**
- The showtime interval in Activity is set to 30 s (120 listing requests per hour), double the 60 per hour that was verified safe. Set it back to 60 s unless the owner chose 30.
- With `BMS_BACKGROUND_LISTINGS=0`, the movie catalogue never refreshes while any alert is active (it was 79 h old). New films and versions do not appear in the picker.
- Alerts match by movie title text. Codex's `movie_key` (now live) handles initials-prefixed titles such as "AOT - Aakasamlo Oka Tara", but other title differences still break matching.
- The movie picker does not show language or format, so a version with no shows (such as Tamil "AOT") looks like the right one.
- No warning when an alert matches 0 shows for its date while BookMyShow lists shows for that film.

**Owner decisions**
- Every check runs in the cloud. No home PC checker, and no extra Railway services or IPs to add throughput (that spreads per-IP load); a second service may only be a standby.
- Ask the owner before deploys, restarts or turning alerts off.
- Planned, not built: showtime checks by priority (changing versions about every 2 min, sold-out or past ones rarely; dates not yet open every 2 to 5 min, not 45), and seat reads focused on the hot show (20 to 25 s, others 4 to 30 min). A slow showtimes ramp (60, 80, 100, 120 per hour, 48 h per step) needs the owner's OK. DeepSeek's answer is in `DEEPSEEK_PARALLEL_PROMPT.md`.

## Current state (5 Nov 2026, Claude Code session)

This section supersedes the older sections below where they conflict. Those sections are kept as history.

- **Hosting:** the old Railway account was removed. Everything now runs on the user's new Railway account: project f7f005f3-75dc-4261-8901-49fc54bd222f, service 18cc5ac2-77e9-4aac-b0d5-d33350495df3, environment production, volume mounted at /data. Public dashboard: https://bmschatgptbot-production.up.railway.app (password protected). Restart policy is ALWAYS.
- **Railway CLI:** `runtime/railway-cli/node_modules/@railway/cli/bin/railway.exe` (browserless login is done). The deploy folder is a copy of the Dockerfile's files plus requirements-cloud.txt, Dockerfile, railway.toml, static/screenwatch.html and deploy/railway-entrypoint.sh. Run `railway up --ci --detach` from that folder. A variable change needs `railway redeploy --yes` to take effect.
- **Credentials:** Telegram and dashboard values are set as Railway variables via a PowerShell 5.1 port of APPLY_CREDENTIALS.ps1. The port sets `[Console]::InputEncoding` to UTF-8 without BOM, because a BOM broke the Telegram token. Never print or paste the values.
- **Verified live:** all three active alerts read seats with HTTP 200, and Telegram delivered alerts with the seat image and the exact show link. One alert (Ramayana, 5 Nov showtime) is paused because that showtime is no longer listed.
- **Source limits:** request budgets live in http_source.py (services-in: alerts 60/hour with a 60 s gap, set on 5 Nov at the user's request as a test; plus a separate `MANUAL` budget of 10/hour, 10 s gap, for Load seat rows, which runs at once outside the alert queue but still honors 429 cooldowns. The dashboard lists queued seat reads with Remove buttons, `GET/POST /api/seat-queue`. Seat-category lookups on in.bookmyshow.com use their own `METADATA` lane (20/hour, 10 s gap) and a 6-hour cache; without it, listing checks took every slot and alert seat reads were silently discarded (fixed 5 Nov). in.bookmyshow.com 60/hour with a 60 s gap, CDN 6/hour). Deadlines carry across deployments through `BMS_SOURCE_NOT_BEFORE` (http_source.carry_over); the user had it deleted on 5 Nov. The dashboard seat panel shows a live "Next read in m:ss" countdown and a **Retry now** button (`POST /api/seat-retry`, `row_watch.clear_source_wait`) that clears saved seat waits; the hourly budget still applies and a new 429 starts a new wait. Do not try to evade BookMyShow or Cloudflare blocks: no TLS impersonation, proxies, IP rotation or header spoofing.
- **Code:** main is at 43f9243 on https://github.com/thrisanjan-pixel/bmschatgptbot. PR #1 (railway-fixes) is merged, and the remote branch `railway-fixes` was deleted on 5 Nov 2026. Recent fixes: worker resilience, Telegram fallbacks, dashboard wording and phone usability, deadline carry-over, and real Cinemas counts in API mode. The Cinemas trend fills in after about two 15-minute samples.
- **Tests on Windows:** `PYTHONPATH=. python runtime/run-checks.py` (needs the tzdata and cryptography packages; run-checks.py is gitignored).
- **GitHub access:** git push and fetch work. GitHub CLI 2.103.3 at `C:\Program Files\GitHub CLI\gh.exe` is signed in as thrisanjan-pixel (browser device login, keyring). The official github plugin fails with "Authorization header is badly formatted" because `GITHUB_PERSONAL_ACCESS_TOKEN` is not set; use `gh` instead.
- **5 Nov later work (main bd195ea, deployed):** a newly picked showtime's seats load first; the page waits for queued seat reads instead of giving up after 90 s; 7 backend alert fixes (no dropped multi-cinema alerts, no lock held during Telegram sends, no double photo/text send, late alerts labelled, edits keep queued alerts, health warnings scale with seat spacing); dashboard JS fixes and a UI polish pass. Seat reads returned HTTP 200 after Retry now.
- **Deploying:** `runtime/cloud-release` is stale. Stage the Dockerfile's file list plus `requirements-cloud.txt`, `Dockerfile`, `railway.toml`, `static/screenwatch.html` and `deploy/railway-entrypoint.sh` in a scratch folder and run `railway up --ci --detach -p <project> -s <service> -e production` there. `railway ssh` needs an SSH key on this PC, which does not exist; the safety check blocks creating one and blocks remote DB writes.
- **Git identity:** none is configured on this PC; commit with `git -c user.name=Thrisu -c user.email=thrisanjan@gmail.com`.
- **Open items:** (1) The live dashboard has NO password (the Dockerfile sets `SCREENWATCH_PASSWORDLESS=1`). On 5 Nov the user chose to leave it open; do not add one unless they ask. (2) Branch `claude/railway-config-ad096c` holds `.railway/railway.ts` replacing `railway.toml` (deprecated, works until 2026-12-01); it is unverified and not merged. (3) `cloud_row_reader.read` sends a Chrome User-Agent, which conflicts with the no-header-spoofing rule; the user has not decided. (4) Check that the Cinemas trend replaced "Collecting history".

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

### Listing queue order (5 Nov 2026)

Listing requests to in.bookmyshow.com are one a minute. `api_watch.next_job` picks: an overdue background listing at most once every 180 s (`BACKGROUND_SPACING`), otherwise alert listings, with the alert movie checked longest ago going first. Dated listings outside every alert's dates count as background. While the request budget waits, the worker picks no job, so waiting never reorders the queue. A broad alert (many formats × dates) shares the budget: each of its listings is checked less often. `/api/status` → `reliability.listing_queue` shows the alert listings with due, last check and error.

Row alerts can set `party_size` (2–10): they alert only when that many seats are free side by side in the chosen rows, and name the most central block. Seat-type lookups wait out a gap of 15 s or less instead of dropping the seat read.

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

## Full audit pass (10 Nov 2026)

Three read-only audits (phone UI, backend, request limits) and their fixes, commits 3232f1f to 1b011d8.

- Phone UI: alert pickers open as a bottom sheet with a Done button on screens up to 680px wide; form fields are 16px there so iOS does not zoom; the search box is not auto-focused on touch devices. The Save bar in the alert form is sticky. The alert list is rebuilt only when what the cards show changes (compared to the minute) and never within 1.5 s of a touch.
- Delivery: a once alert completes after its last queued message, not its first. `telegram.claim` hides an outbox row for 120 s before sending so two workers cannot both send it. A Telegram 429 sets `telegram_blocked_until` and pauses all sends.
- Scheduling: seat spacing is 60 s to match the source gap, and a budget wait never changes a job's due time. Background listings get at most one turn in five (`BACKGROUND_SPACING=300`) and get the turn back when no request was made. A never-checked alert listing joins the end of the overdue line. "Retry now" clears only the budget and spacing waits, never a 429 Retry-After deadline.

Left open on purpose:
- The hosted dashboard has no password (`SCREENWATCH_PASSWORDLESS=1`). Anyone with the link can change alerts. Owner's decision.
- Two alerts with different intervals on the same listing can repeat a line, because the snapshot baseline is shared between alerts.
- `watch_preferences.rules()` re-reads every job for every alert on each call; slow with many alerts.
- The health label for a showtime alert uses the newest check of any of the movie's listings, so one healthy listing can hide a failing one.

## New alert features (10 Nov 2026, commit 951a06e)

- Any-showtime row alert: `row_show='*'` watches the chosen rows at every matching showtime of exactly one theatre, up to 20 showtimes (`ROW_ANY_LIMIT`). Seat memory is kept per showtime in `row_alert_state` under `alert_id:show_id`. The seat budget is unchanged (60 reads an hour), so with N watched showtimes each is read about every N minutes.
- Urgent alerts (`urgent`): their listings count overdue time three times, and their showtimes jump the seat queue after 40 s instead of 2 minutes.
- Presets: stored in meta `alert_presets`; POST actions `save_preset` and `delete_preset`.
- History: GET `/api/watch` gives each alert its last 10 outbox messages.
- Install as an app: `/manifest.json` and `/static/icon-*.png` are served without login.
- Verified live after deploy 5f6ae4f2: one '*' alert watching 2 showtimes, no health errors, no console errors. Not checked on a real iPhone; home-screen install untested.
- Open: state rows of '*' alerts for showtimes that have ended are not cleaned up. The "Watch next date" note still says to pick rows again for '*' alerts.

## Open on PC (10 Nov 2026, commit f6149c8, deploy 39c459a3)

- Each alert has an "Open on PC" switch. `pc_opener.py` on the owner's PC polls `/api/pc-alerts` (every 3 s while a switch is on, every 30 s otherwise). For the alert's next message from the last 10 minutes, it opens the BookMyShow link in the default browser, sounds an alarm and shows a pop-up, then posts `pc_opened` to turn the switch off.
- The helper only talks to the dashboard. It never requests BookMyShow and never clicks anything; it opens only `https://in.bookmyshow.com/` links.
- Start it with `start_pc_opener.bat` (pythonw, no console). It is not added to Windows startup.
- Automated seat holding or checkout from Railway or the PC was declined: it needs getting past BookMyShow's bot block (the checkout lab got HTTP 403 on Railway).
- Railway CLI is now signed in as Thrisu; it had been signed in to another account (ItsKG) that cannot access the project.

## Chrome seat helper replaces the PC opener (11 Nov 2026, commit 8ffaa91, deploy 0b4f5146)

- `pc_opener.py` and `start_pc_opener.bat` were removed on the owner's request. The Chrome extension in `chrome-extension/` is the only PC client. The "Open on PC" notes above describe the removed helper.
- "Open on PC" arms an alert with no time limit (until it opens once or is turned off; changed 24 Nov 2026) and needs the extension to have polled in the last 2 minutes. The extension opens the showtime in Chrome, highlights the two seats and selects them only when the owner presses "Select these two seats". It never presses Pay, terms or sign-in controls.
- Install: `chrome://extensions`, Developer mode, Load unpacked, choose `chrome-extension`, then switch "Enabled on this PC" on in the pop-up. See `chrome-extension/README.md`.
- Verified on 11 Nov 2026 (deploy 070c6ba5, commit 6456865): the owner loaded the extension in their Chrome, a real Ramayana row alert opened the seat page and ringed the pair, and the owner confirmed it worked.
- Fixed the same day: the first message of a new row selection ignores the repeat gap, and a row alert opens on PC only for its row messages (those starting with the ticket emoji).
- The overview shows an "Extension connected / not connected" button, refreshed every 15 seconds.

## Minimum seats and category seat photos (10 Nov 2026, commit 2be058a, deploy 00e33ee0)

- `min_seats` (0 = off, 1–50): an alert fires only when at least that many seats are open in its chosen rows or opened categories.
- Category alerts send their text at once, then a "📸" follow-up with the seat-map photo and per-category counts after the next seat read. With `min_seats` set, they wait in `pending_seat_photos` (mode 'gate') and send one message when the minimum is met.
- Photo reads share the 60-an-hour seat budget and go ahead of routine reads.
- `notifications_paused` means "no alert is enabled", not a manual pause, so follow-ups still go out after a once alert finishes.
- Open on PC ignores "📸" follow-ups, so the page is not opened twice.
- Not yet seen on a live alert: the first real category opening will show whether the photo arrives about a minute later.
