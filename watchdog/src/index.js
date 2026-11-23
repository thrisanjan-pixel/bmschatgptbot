// Screenwatch watchdog: a Cloudflare Worker with one Durable Object.
// The Durable Object re-arms its own alarm() every INTERVAL_SECONDS, so it keeps running
// without a request. The Cron trigger only re-arms the alarm if it was ever lost.
import { afterPromote, decide, view } from './decide.js';

const TIMEOUT_MS = 10000;

async function post(url, secret, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${secret}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    let data = {};
    try { data = await response.json(); } catch { data = {}; }
    return { ok: response.ok, status: response.status, role: data.role, data };
  } catch (error) {
    return { ok: false, status: 0, error: String(error && error.name || error) };
  } finally {
    clearTimeout(timer);
  }
}

async function notify(env, message, priority) {
  if (!env.NTFY_TOPIC) return;
  const base = (env.NTFY_URL || 'https://ntfy.sh').replace(/\/$/, '');
  try {
    await fetch(`${base}/${env.NTFY_TOPIC}`, {
      method: 'POST',
      headers: { Title: 'Screenwatch watchdog', Priority: priority || 'high', Tags: 'rotating_light' },
      body: message,
    });
  } catch { /* the next tick tries the next alert */ }
}

function authorized(request, env) {
  const secret = env.HEARTBEAT_SECRET || '';
  const given = request.headers.get('Authorization') || '';
  const expected = `Bearer ${secret}`;
  if (secret.length < 32 || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export class Watchdog {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  interval() {
    return Math.max(30, Number(this.env.INTERVAL_SECONDS || 60)) * 1000;
  }

  async ensureAlarm() {
    if ((await this.ctx.storage.getAlarm()) == null) await this.ctx.storage.setAlarm(Date.now() + 1000);
  }

  async fetch(request) {
    await this.ensureAlarm();
    const url = new URL(request.url);
    if (url.pathname === '/status') return Response.json((await this.ctx.storage.get('state')) || {});
    return new Response('armed');
  }

  async alarm() {
    try {
      await this.tick();
    } finally {
      // Re-arm first thing after the work, even when the tick threw.
      await this.ctx.storage.setAlarm(Date.now() + this.interval());
    }
  }

  async tick() {
    const env = this.env;
    const now = Math.floor(Date.now() / 1000);
    let state = (await this.ctx.storage.get('state')) || null;
    const shared = { from: 'watchdog', instances: state ? view(state) : {} };
    const [primary, standby] = await Promise.all([
      post(`${env.PRIMARY_URL}/heartbeat`, env.HEARTBEAT_SECRET, shared),
      post(`${env.STANDBY_URL}/heartbeat`, env.HEARTBEAT_SECRET, shared),
    ]);
    const decision = decide(state, { now, primary, standby }, { graceSeconds: Number(env.GRACE_SECONDS || 180) });
    state = { ...decision.state, lastTick: now };
    for (const action of decision.actions) {
      if (action.type === 'notify') {
        await notify(env, action.message, action.priority);
      } else if (action.type === 'promote') {
        const result = await post(`${env.STANDBY_URL}/promote`, env.HEARTBEAT_SECRET, { reason: 'watchdog: primary missed heartbeats' });
        const after = afterPromote(state, result);
        state = { ...after.state, lastTick: now };
        for (const next of after.actions) await notify(env, next.message, next.priority);
      }
    }
    await this.ctx.storage.put('state', state);
  }
}

export default {
  async fetch(request, env) {
    if (!authorized(request, env)) return new Response('Unauthorized', { status: 401 });
    const stub = env.WATCHDOG.get(env.WATCHDOG.idFromName('main'));
    return stub.fetch(request);
  },
  async scheduled(_event, env, ctx) {
    const stub = env.WATCHDOG.get(env.WATCHDOG.idFromName('main'));
    ctx.waitUntil(stub.fetch('https://watchdog/arm'));
  },
};
