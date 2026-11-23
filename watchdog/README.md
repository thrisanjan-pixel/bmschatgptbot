# Screenwatch failover watchdog

A Cloudflare Worker that watches the primary and standby Screenwatch instances
and promotes the standby when the primary stops answering. It runs on the
Workers free plan.

## How it works

- One Durable Object (`Watchdog`) owns all state. Its `alarm()` runs a tick
  every `INTERVAL_SECONDS` (default 60) and always re-arms itself in a
  `finally` block, so one failed tick never stops the loop.
- A cron trigger (`*/5 * * * *`) only re-arms the alarm if it was ever lost.
- Each tick POSTs `/heartbeat` to both instances with
  `Authorization: Bearer $HEARTBEAT_SECRET`, then feeds the results to the pure
  function `decide()` in `src/decide.js`.
- `decide()` rules:
  - Primary unreachable or not ready for longer than `GRACE_SECONDS`
    (default 180) and standby reachable: POST `/promote` to the standby. It
    retries every tick until the standby reports `active`.
  - Promotion succeeds: one ntfy alert, "Primary lost; standby promoted."
  - Promotion fails: one ntfy alert per episode.
  - Both unreachable past the grace period: one urgent ntfy alert per episode,
    no promotion.
  - Both report `active`: one ntfy warning per episode.
  - It never fails back on its own. Failback is a manual `/demote` (see the
    root README runbook).

## Deploy

```bash
cd watchdog
npx wrangler login
npx wrangler secret put HEARTBEAT_SECRET
npx wrangler secret put NTFY_TOPIC
npx wrangler deploy
```

Before `deploy`, set `PRIMARY_URL` and `STANDBY_URL` in `wrangler.toml` to the
two public HTTPS base URLs. `HEARTBEAT_SECRET` must be the same value (at least
32 characters) on both bot instances and the Worker. Never put secrets in
`wrangler.toml`.

After deploy, start the alarm once:

```bash
curl -X POST -H "Authorization: Bearer $HEARTBEAT_SECRET" https://screenwatch-watchdog.<your-subdomain>.workers.dev/arm
```

`GET /status` with the same header returns the watchdog's current view.

## Test

```bash
npm test
```

The tests cover `decide()` only (grace period, retries, both-down, both-active,
no automatic failback). They need Node 18 or newer and no network.
