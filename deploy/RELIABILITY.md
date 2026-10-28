# Monitoring and continuity

Active movie jobs take priority, with an overdue background turn after five minutes. Row jobs share one durable queue entry per show; on-demand loads get a turn after 90 seconds. Existing source cooldowns remain respected.

check_stats stores attempts, successes, failures, latency and successful-check gaps for 30 days. Gaps include outages and restarts; the 30-second seat interval is a target, not a guarantee.

The independent health_watch.py process runs every 15 seconds within the same Railway service. After a two-minute startup grace it warns once per sustained issue: listing heartbeat older than 90 seconds, an actively used row worker older than 120 seconds, no successful active-alert check for five minutes, or failed Telegram delivery queued over three minutes. It sends one recovery per cleared issue. Messages persist in the outbox and can send while normal alerts are paused. A Telegram outage delays the warning itself.

/healthz remains the lightweight process health endpoint used by Railway restarts. /readyz returns 503 when the monitor or watchdog is stale or has recorded a sustained issue. Point an independent external uptime monitor at /readyz to detect a whole-service outage. No external monitor is configured by this deployment; the internal watchdog cannot report a total Railway outage.

Railway billing is not connected. The dashboard explicitly reports unknown allowance and links to the account plans page. Optionally set SCREENWATCH_HOSTING_ENDS_AT to a verified timezone-aware ISO expiry, for example 2026-11-20T12:00:00Z. The dashboard and watchdog warn within three days. Do not guess the date. This does not report credit balance, prevent exhaustion, buy credits or upgrade a plan.
