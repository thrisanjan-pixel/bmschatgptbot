# Railway background worker

Railway can run this bot continuously as a Docker worker. It is not guaranteed free hosting. Check your account's current trial balance, expiration, subscription requirements and usage rates before activating it. Running five Chromium workers continuously consumes metered memory/CPU; a subscription or credit allowance is not a fixed total bill. Railway may stop the service when credits run out or an account limit is reached. No paid deployment has been created from this workspace.

## Deploy

1. Create or sign in to your own Railway account and connect GitHub. These files are currently local to this workspace; get the reviewed source into your repository before choosing **Deploy from GitHub repo**, or upload the working directory with Railway's CLI from an authenticated machine. Do not upload cloud credential placeholders or the development database as source code.
2. Select this repository's root. `railway.toml` selects the supplied Dockerfile, a single worker replica and restart-on-failure. Deploy the background worker; do not use a cron schedule. Keep Railway's sleeping/serverless option disabled for continuous polling.
3. Attach a persistent volume mounted at **`/data`**. Keep **`BMS_DB=/data/state.sqlite3`** (the image default). Without the volume, redeployment loses baselines and pending alerts. The entrypoint sets ownership of the mount directory and runs the worker as the unprivileged `app` user.
4. Add **`TELEGRAM_BOT_TOKEN`** and **`TELEGRAM_CHAT_ID`** using Railway's secure service variables. Use your actual Telegram credentials from secure storage, not the development proxy's placeholder values. Never paste values into Dockerfile, repository files, CLI arguments or chat. If the token originally shared in chat has not been rotated, replace it through BotFather and use the replacement.
5. Allow outbound HTTPS to BookMyShow and Telegram. Do not copy this development environment's proxy or private certificate setup to Railway. Normal public TLS validation remains enabled. Pick the closest available region; Railway availability and BookMyShow access must be checked in that actual region.
6. Deploy one instance. Leave public networking/domain creation off: this image runs only the bot, not the private dashboard. Inspect logs for `catalog checked`, `movie checked`, `discovery checked`, `venue checked` and `seat checked`. HTTP 403/429/5xx cause cooldown, not successful source observations. Logs must not print tokens.
7. In a Railway service shell, inspect status without secrets:

   ```sh
   python -c "from store import connect,coverage,meta; import json,time; c=connect('/data/state.sqlite3'); print(json.dumps({'state':meta(c,'city_state'),'heartbeat_age':time.time()-float(meta(c,'city_heartbeat','0')),'coverage':coverage(c)}))"
   ```

   A fresh heartbeat alone does not establish full coverage. Verify successful checks increase and source errors are resolved. The first inventory is silent. A labelled setup notification can verify delivery; do not misrepresent a test as a real new show/release.
8. Restart/redeploy once and verify the same database and snapshots remain. Then confirm a genuine later source change delivers its formatted alert. Do not run the development monitor and Railway monitor against the same Telegram chat simultaneously if you want to avoid independent duplicate alerts. Move service ownership only after the Railway instance passes these checks.

## Operational limits

The deployment assets are prepared; an image build, live Railway deployment, billing verification, volume persistence and BookMyShow access from Railway remain to be validated in your account. Source HTTP 503 errors observed in development may also occur on Railway. Moving hosting does not guarantee their resolution.

Local validation: TOML parsing and entrypoint shell syntax passed. The attempted Docker build stopped while fetching `python:3.12-slim-bookworm` because Docker Hub's registry returned HTTP 503; no image or container smoke-test success is claimed.

Cinema/seat rechecks become eligible after 30 seconds; queue length and source cooldown can extend an all-city cycle to many minutes or hours. This remains polling and cannot promise instant citywide alerts. A persistent volume and an always-on Railway service provide a deployment mechanism, not a guarantee of uptime or source availability.
