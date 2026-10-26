# Railway: browser-free bot and private mobile dashboard

The root Dockerfile runs the API worker and dashboard together, without Chromium or pip dependencies. One replica uses SQLite on a persistent /data volume. Railway provides HTTPS for the generated domain.

## Current deployment status

On 2026-10-23 the account showed $5 trial credit, no payment method, and shutdown when credits run out. Creating a project failed with "Access Denied: Deploys have been paused temporarily". No Railway service or public URL has been created. Do not claim the container or Railway source access is verified until deployment succeeds.

## Deploy when Railway permits

1. Select thrisanjan-pixel/bmschatgptbot, main. Keep the Trial/Free plan; do not add a card or upgrade.
2. Attach a volume at /data. Keep one replica and sleeping disabled for continuous monitoring.
3. Set DASHBOARD_PASSWORD to a unique password of at least 16 characters through Railway's private Variables editor. The username is screenwatch. Never place credentials in chat, Git, URLs, or logs.
4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID through the same private editor. Stop the local monitor before enabling cloud Telegram to avoid duplicate notifications.
5. Deploy, generate a Railway domain, and open its HTTPS URL on the phone. Authenticate using screenwatch and the configured password. The public health check is /healthz; all dashboard data requires authentication. Hosted /setup is disabled even with authentication.
6. Verify successful source reads, a fresh worker heartbeat, Telegram delivery, mobile navigation, and database persistence across a redeploy. Health check success alone does not prove the source or Telegram works.

Credits are finite and uptime is not guaranteed. Source restrictions may differ on Railway. First observations are silent; category availability is supported, individual seats and maps are not. Do not run two independent notifying workers for the same chat.

## Railway progress — 2026-10-26

Railway now accepts deployments. Created project `209eae69-d37c-45e7-83d1-e65c58b28794` (resplendent-light), service `c31aa42e-3e04-4efc-bebc-5d3576dce675`, production environment `27211a0d-0fa2-488d-a730-ce519d70a4a4`. Billing verified: $5 remaining, 27 days, no payment method; deployment shuts down when credits run out. No plan upgrade.

Published main commit f6b9568 after 47 passing tests. Attached volume `0026c719-4183-4619-a3df-9b7c545b5904` at /data and configured /healthz through the Railway UI. New services no longer use railway.toml Config-as-Code, so UI configuration is required. One replica, serverless disabled. Build passed and deployment is pending dashboard password/health check. No public domain generated yet.

User handoff pending: Railway Variables form has DASHBOARD_PASSWORD name prepared. User must enter and submit a password >=16 characters locally, then reply saved without disclosing it. Do not inspect the password value. Telegram variables are not configured; alerts remain disabled by default. After password setup, verify deployment, generate domain, check authentication and real source reads, and configure Telegram securely. Row reader is PC-only in current Docker image. Do not claim the website or Telegram is ready until verified.

### Follow-up startup diagnosis

The first f6b9568 deployment failed its health check. Deploy logs explicitly report DASHBOARD_PASSWORD below the required 16 characters; api_watch started and performed discovery, then encountered source HTTP 403 and backed off. Do not describe the cloud monitor as fully operational.

A pending service deletion was encountered during the follow-up, cancelled before confirmation, and undone with Keep Service. The service and /data volume remain intact. User is correcting the password directly in Railway; no secret was read or copied. Public domain generation was blocked by automatic approval review until authentication is verified. No domain was created. After the corrected password is saved, deploy the variable change, verify dashboard startup and health, then generate a domain and validate HTTP authentication. Never lower the password requirement to bypass this blocker.

### Live website verified

Corrected password deployment f47807ff-b7dd-48fe-9827-1b7990bc2cd7 passed /healthz. Generated https://bmschatgptbot-production.up.railway.app with target port 8000; explicitly set PORT=8000. Main f93e3c9 adds a mobile/in-app compatible sign-in form with 8-hour Secure/HttpOnly/SameSite=Strict sessions, CSRF protection and login throttling; 48 tests passed. Live sign-in succeeded. Unauthenticated /api/status remains 401; dashboard inventory showed 28 movies, 96 cinemas and 3797 future screenings, 179/213 showtime pages checked, zero alerts delivered. BookMyShow occasionally returns 403 and the worker correctly enters cooldown; source checks are not guaranteed uninterrupted. Telegram token/chat are still absent on Railway. Consent request pending to transfer existing private PC credentials directly to Railway and send one labelled connection test, keeping watch alerts paused. Rows remain PC-only.
