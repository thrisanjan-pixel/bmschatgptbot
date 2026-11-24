# Screenwatch handoff between Claude and Codex

Use the latest Screenwatch branch or PR named by the owner as the source of truth. The PC handoff checkout may be older and may contain local experiments. A proposed change stays pending until a commit or PR and its test result are recorded.

- Before editing, check the current branch, working tree, and remote tip. If the other assistant has work in progress in the same files, use a separate branch or worktree and record the overlap here.
- Keep each change small and on its own branch. Preserve uncommitted work. Review the other branch's diff before combining changes, especially where both edits touch the alert feed, PC helper, Chrome extension, or dashboard controls.
- For a handoff, record the repo, branch or PR, commit SHA, files changed, verification, and work still pending. Use the same information to coordinate the next deployment. Have one assistant handle a given deployment and record the Railway project, service, and deployed commit after verification.
- Keep tokens, passwords, browser sessions, and payment details out of this note.

Current handoff: Claude is working on the one-click seat-selection flow in the newer Screenwatch workspace. Its branch, commit, and live test result are pending. Codex should review that revision before making any follow-up changes. Railway sign-in is separate from GitHub access. Confirm the production project and service in Railway before deploying.

## Update (25 Nov 2026, ~00:20 IST, Claude Code)
- Live is Railway deployment `75f814d6-ff77-4969-a77d-e7c9a463fdc6` = branch `feat/sold-out-rotation` (PR #22, open), on top of main `2310b8d`.
- Railway variable `BMS_SOLD_OUT_RECHECK` is now `0` (was 180). With the new code, 0 turns the sold-out backstop off: a sold-out answer counts as a normal read, and every 20 s seat slot rotates through all watched showtimes in turn with no idle wait. With 4 watched AMB showtimes each is read about every 80 s. Old code would treat 0 as 120 s, so do not roll back the code without resetting this variable to 180.
- After deploy: healthz and readyz 200, seat requests 20-21 s apart, all HTTP 200.
- The user asked for every seat alert every 20 s. That is not possible per show: the seat host allows one read per 20 s in total (15 s and 10 s gaps got 21-hour 429 blocks), so the gap per show is 20 s times the number of watched showtimes.

## Update (24 Nov 2026, ~23:55 IST, Claude Code)
- Live is Railway deployment `97c7e0d3-3b7a-46e4-8cc2-42886a418e40`, built from `feat/overview-layout`. PR #19 (split queue) and PR #20 (overview layout) are both merged; main `a5d5ad2` matches live code. Deploy from main.
- Change: Overview is full width (`.wrap` max 1560 px), monitor status and the three counts sit in one compact row, alert cards use a two-column grid beside a wider check queue (440-560 px), and active alerts are listed before drafts, missed, completed and cancelled. CSS is in the `<style id="sw-layout-v2">` block and applies from 1100 px; the phone layout is unchanged.
- Local preview with live data: `D:/bms tracking bot/rr-tmp/proxy.py 8768` (launch config `layout-live`) serves the worktree page and forwards read-only `/api` GETs to production.

## Update (24 Nov 2026, ~23:30 IST, Claude Code)
- Live is now Railway deployment `41ba3064-6a02-43d2-8465-5743654c3342` = branch `fix/seat-queue-eta` commit `6e0e50a` (PR #19, open), on top of main `6938ca4` (PR #18 merged).
- Change: the check queue shows two lanes side by side, Showtimes (listing interval) and Seats (seat gap). `/api/check-queue` returns up to 6 upcoming and 5 recent per kind, plus `seat_gap`. Seat ETAs use the real seat gap (`policy(SEAT_HOST)`), not a fixed 60 s. Panel width 300 to 520 px.
- Note: seat requests really go out every 20-21 s. The queue only lists reads already scheduled, and "Just checked" keeps one row per showtime (`check_stats`), so a hot showtime that is re-read every ~40 s fills the gaps without showing every read.
- After deploy: healthz and readyz 200, seat_gap 20 on the live API.

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

## Handoff from Claude, 11 Nov 2026

- Repo `thrisanjan-pixel/bmschatgptbot`, branch `main`. The commit "Add the Chrome seat helper" holds the server support and the new `chrome-extension/` folder. It builds on the uncommitted `pc_seat_pair` change that was in the working tree.
- Files: `watch_preferences.py`, `dashboard.py`, `pc_opener.py`, `static/screenwatch.html`, `tests/test_features.py`, `chrome-extension/*`.
- Verification: 290 Python tests and 14 node tests pass. The page script passed against the fake seat map in `chrome-extension/test/fixture.html`. The Konva facts it relies on were checked by hand on a live BookMyShow seat page.
- Pending: deploy to Railway (project f7f005f3, service 18cc5ac2) is waiting for the owner's approval. The extension has not been loaded in real Chrome yet, so the banner, the handshake and a real alert are unverified.
- Update, 11 Nov 2026: `pc_opener.py` and `start_pc_opener.bat` were removed on the owner's request. Only the Chrome extension counts as a PC client now; 289 Python tests and 14 node tests pass.
