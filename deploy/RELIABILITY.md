# Monitoring and continuity

Active movie jobs take priority, with an overdue background turn after five minutes. Row jobs share one durable queue entry per show; on-demand loads get a turn after 90 seconds. Existing source cooldowns remain respected.

check_stats stores attempts, successes, failures, latency and successful-check gaps for 30 days. Gaps include outages and restarts; the 30-second seat interval is a target, not a guarantee.

The independent health_watch.py process runs every 15 seconds within the same Railway service. After a two-minute startup grace it warns once per sustained issue: listing heartbeat older than 90 seconds, an actively used row worker older than 120 seconds, no successful active-alert check for five minutes, or failed Telegram delivery queued over three minutes. It sends one recovery per cleared issue. Messages persist in the outbox and can send while normal alerts are paused. A Telegram outage delays the warning itself.

/healthz remains the lightweight process health endpoint used by Railway restarts. /readyz returns 503 when the monitor or watchdog is stale or has recorded a sustained issue. Point an independent external uptime monitor at /readyz to detect a whole-service outage. No external monitor is configured by this deployment; the internal watchdog cannot report a total Railway outage.

Railway billing is not connected. The dashboard explicitly reports unknown allowance and links to the account plans page. Optionally set SCREENWATCH_HOSTING_ENDS_AT to a verified timezone-aware ISO expiry, for example 2026-11-20T12:00:00Z. The dashboard and watchdog warn within three days. Do not guess the date. This does not report credit balance, prevent exhaustion, buy credits or upgrade a plan.


## Controlled cloud seat probe — 5 November 2026

A single operator-approved read of the active AMB screening from the Railway container returned HTTP 429. The source response included Retry-After: 33792, Server: cloudflare, Content-Type: application/json; charset=utf-8. This confirms a current source rate limit, rather than only a stale local timer. The response retry was persisted and repeated manual reads were stopped. Temporary Railway SSH credentials were revoked afterward.

Seat polling now targets 60 seconds per screening with at least 30 seconds between cloud layout reads. For 15 minutes after a source restriction expires, targets slow to 120 seconds per screening and at least 60 seconds between reads. The recovery window starts after the restriction ends, so long retry periods do not skip it. These are scheduling targets, not guaranteed successful read intervals. Shared reads remain deduplicated across row alerts. No proxy rotation or automatic cooldown resets are used.


## TLS-client comparison — 5 November 2026

A single source-eligible listing request previously refused with HTTP 403 returned HTTP 200 and a validated native showtime response using curl_cffi 0.13.0 with its Safari TLS profile from the same Railway container. That result does not prove that all refusals were fingerprint-related or that the seat rate limit has cleared. The experimental compatible client is not activated in production. The deployed client retains urllib and source refusals are honored. It preserves HTTP status and Retry-After, disables redirects and limits responses to 8 MiB. The old custom User-Agent is omitted so the library can use headers matching its TLS profile. Production metadata reports whether persistent request budgeting is enabled. The existing seat source wait is retained, and fresh row access remains unverified until another permitted read.


## Persistent source budgets

Production reserves every source request transactionally in SQLite before dispatch. Seat-layout host requests are limited to six per rolling hour with at least 600 seconds between calls. Listing host requests are limited to sixty per rolling hour with at least sixty seconds between calls; catalogue requests are limited to six per rolling hour. Refused and failed attempts consume the budget. HTTP 429 Retry-After is stored as an absolute not-before time and survives restarts. Budget deferrals are not recorded as failed upstream checks. The request log keeps seven days of host, endpoint path, time, HTTP status, Retry-After and CF-Ray. Cookies, credentials, request bodies and response bodies are not logged. These initial experimental budgets are not claimed to be BookMyShow-approved limits or proof of reliable access.
