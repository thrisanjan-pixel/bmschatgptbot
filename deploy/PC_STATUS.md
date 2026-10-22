# PC continuation status — 2026-10-22

No paid resources were activated and no secrets were requested in chat.

## Latest: browser-free replacement active

The browser-based supervisor and its workers were stopped at the user's request. The replacement is `api_watch.py`, supervised with `supervise.py --api`, launched using `deploy/run-api.sh`. It uses `runtime/api.sqlite3`, a separate durable database, and the saved local credentials outside Git. No browser windows are used by the replacement.

Verified: direct HTTP catalogue fallback and showtime API reads, 31 passing tests, a fresh heartbeat with Telegram enabled, an accepted labelled readiness test through the durable outbox, and automatic restart of the worker with snapshots retained. Last observed partial inventory: 28 movies, 85 cinemas, 3,183 future screenings; 88 of 195 queued showtime pages had been checked. Counts grow as discovery proceeds. No current source errors were present at this check. An actual future source transition is not manufactured as a test.

The dashboard at http://localhost:8000 now has a responsive layout, movie/show search, date filtering, recent delivery activity, and explicit Telegram/source status. Desktop and 390px mobile checks passed with no horizontal overflow or JavaScript errors.

The source-owned CDN catalogue cache is used because the website's discovery endpoint returned HTTP 500 to normal direct reads. This is the same public fallback referenced by its client; it may lag live catalogue updates. Show and category availability are supported; individual seats/rows/maps are not included in this simplified replacement. Railway deployment remains inactive. The existing Dockerfile still runs the older browser worker and must be deliberately updated and tested before deploying the API replacement there.

Stop: `wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/run-api.sh' stop`.

Older sections below are historical and superseded by this update.

## Update after Windows restart

### Local preview running

Started `deploy/local-preview.sh` in Ubuntu. A notification-free monitor and loopback dashboard run against `runtime/preview.sqlite3`; logs and PID files remain in ignored `runtime/`. The dashboard is accessible from Windows at http://localhost:8000. A fresh heartbeat was verified, but the catalogue request returned HTTP 403 and entered the persisted five-minute cooldown. No movies/shows were successfully scanned and no Telegram delivery was attempted. Keep the PC awake for this local process; there is no boot-start service configured.

Start: `wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/local-preview.sh'`

Stop: `wsl -d Ubuntu -u root -- bash '/mnt/d/bms tracking bot/bmschatgptbot-pc-handoff/deploy/local-preview.sh' stop`

The Windows sandbox/browser-control problem was traced to `.git` ownership by CodexSandboxOffline. The user repaired ownership and browser controls now work. Railway shows a verified trial with no payment method; deployment creation was blocked by “Deploys have been paused due to an upstream issue.”

- WSL 3.0.3 starts successfully with kernel 6.18.54.1. Firmware virtualization is enabled and the Windows restart flag is clear.
- Installed Ubuntu 26.04.1 LTS using the official WSL web-download option in an interactive terminal. Noninteractive installation attempts stalled and were stopped before retrying.
- Initial Ubuntu filesystem reported approximately 1.3 GB used before adding dependencies. This is disk use, not a measured download size.
- Installed Python virtual-environment support, pinned Playwright 1.62.0, Chromium and browser system dependencies. The environment is `/root/.local/share/bms-verify/venv` in Ubuntu.
- All 21 behavioral tests passed under Ubuntu Python 3.14.4.
- A bounded, notification-free diagnostic used `/tmp/tmp.ZopclZrC0H/state.sqlite3`. Chromium reached BookMyShow, which returned HTTP 403. The worker logged a 300-second cooldown; successful live catalogue coverage is not established.
- `deploy/verify-wsl.sh` reproduces local dependency setup, tests and a 60-second diagnostic with a separate temporary database. Install `python3-venv` first; browser system dependency installation requires root/sudo.
- Railway deployment remains inactive. Limited Trial outbound restrictions, image validation and a sustainable zero-payment operating budget remain unresolved. No Telegram credentials are configured by this validation script.

The earlier observations below describe the initial inspection before the restart; the update above supersedes their WSL/test blockers.

## Verified

- Windows reports build 26200, release 25H2.
- WSL launcher reports WSL is not installed. Both standard installation and distribution-specific installation returned the same not-installed message; installation did not complete.
- Docker, Python, Git, Railway CLI and Node were not found on the ordinary command path. Codex bundled Python and Git are available.
- Unit test discovery fails importing `fcntl` on Windows. This is a runtime incompatibility, not a passing test run. Playwright is also absent from bundled Python.
- PyPI confirms the pinned Playwright 1.62.0 exists.
- GitHub repository thrisanjan-pixel/bmschatgptbot is private and empty; approved `git ls-remote` returned no refs. The connected GitHub account has push permission.
- Restored the supplied Git history without replacing working files. Published `main` to the empty private GitHub repository successfully, including the handoff and status report. Transfer bundle remains local and untracked.
- Railway sign-in succeeded. Usage page shows $0 used, $5 trial credits, 1 GB RAM and 2 vCPU. Account is a Limited Trial, expiring after 30 days or credit exhaustion.
- Railway displayed an active platform-outage banner during inspection.

## Remaining blockers

1. Complete WSL installation in an administrator Windows terminal and reboot if requested. Then install Ubuntu/Python/Chromium and run the tests and bounded dry-run with a disposable database. Native Windows execution is not supported by the current code.
2. The restricted Windows command environment began failing to start processes with `setup refresh had errors`; approved execution worked for the local bundle fetch. Do not interpret this as an application failure.
3. Railway Limited Trial restricts outbound networking. Free verification must succeed before assuming BookMyShow/Telegram reachability. No upgrade or billing commitment is authorized.
4. No image build, live bot check, Telegram delivery check, deployment, or volume-persistence check has passed on this PC.
5. Trial credits do not establish sustainable free 24/7 operation for five Chromium workers. Confirm zero-payment limits and resource use before activating any service.

Official references: https://docs.railway.com/pricing/free-trial and https://docs.railway.com/volumes/reference
