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
