// Pure failover decisions for the Screenwatch watchdog. No I/O here, so it is unit-tested.
//
// decide(state, observation, config) -> { state, actions }
//   observation: { now, primary: { ok, role }, standby: { ok, role } }   (now in seconds)
//   actions:     { type: 'promote' } | { type: 'notify', key, message, priority }
// afterPromote(state, result) -> { state, actions }  once the runtime has called /promote.
//
// Rules: primary missed for more than graceSeconds while the standby answers -> promote it
// (retried each tick until it reports active). Both missed -> "Both instances down." once per
// episode. Both answering as active -> warn once per episode. Never fails back on its own.

export const MESSAGES = {
  promoted: 'Primary lost; standby promoted.',
  promoteFailed: 'Primary lost; promoting the standby FAILED. Check both instances now.',
  bothDown: 'Both instances down.',
  bothActive: 'Both instances report active: alerts may be sent twice. Demote one (POST /demote).',
};

export function initialState(now) {
  return {
    startedAt: now,
    primaryLastOk: null,
    standbyLastOk: null,
    primaryRole: null,
    standbyRole: null,
    lastPromoteAttempt: null,
    notified: { primaryLost: false, promoteFailed: false, bothDown: false, bothActive: false },
  };
}

function copy(state, now) {
  const base = state && typeof state === 'object' ? state : initialState(now);
  return { ...initialState(now), ...base, notified: { ...initialState(now).notified, ...(base.notified || {}) } };
}

export function decide(state, observation, config = {}) {
  const now = observation.now;
  const grace = Number(config.graceSeconds ?? 180);
  const s = copy(state, now);
  const actions = [];
  const primary = observation.primary || { ok: false };
  const standby = observation.standby || { ok: false };

  if (primary.ok) { s.primaryLastOk = now; s.primaryRole = primary.role ?? null; }
  if (standby.ok) { s.standbyLastOk = now; s.standbyRole = standby.role ?? null; }
  const primaryDownFor = now - (s.primaryLastOk ?? s.startedAt);
  const standbyDownFor = now - (s.standbyLastOk ?? s.startedAt);
  const primaryLost = !primary.ok && primaryDownFor > grace;

  // A primary that answers again ends the "lost" episode (failback itself stays manual).
  if (primary.ok) { s.notified.primaryLost = false; s.notified.promoteFailed = false; }

  if (!primary.ok && !standby.ok) {
    if (primaryDownFor > grace && standbyDownFor > grace && !s.notified.bothDown) {
      actions.push({ type: 'notify', key: 'bothDown', message: MESSAGES.bothDown, priority: 'urgent' });
      s.notified.bothDown = true;
    }
  } else {
    s.notified.bothDown = false;
  }

  if (primaryLost && standby.ok && standby.role !== 'active') {
    actions.push({ type: 'promote' });
    s.lastPromoteAttempt = now;
  }

  if (primary.ok && standby.ok && primary.role === 'active' && standby.role === 'active') {
    if (!s.notified.bothActive) {
      actions.push({ type: 'notify', key: 'bothActive', message: MESSAGES.bothActive, priority: 'high' });
      s.notified.bothActive = true;
    }
  } else {
    s.notified.bothActive = false;
  }
  return { state: s, actions };
}

export function afterPromote(state, result) {
  const s = copy(state, 0);
  const actions = [];
  if (result && result.ok && result.role === 'active') {
    s.standbyRole = 'active';
    if (!s.notified.primaryLost) {
      actions.push({ type: 'notify', key: 'primaryLost', message: MESSAGES.promoted, priority: 'urgent' });
      s.notified.primaryLost = true;
    }
  } else if (!s.notified.promoteFailed) {
    const detail = result && result.status ? ` (HTTP ${result.status})` : '';
    actions.push({ type: 'notify', key: 'promoteFailed', message: MESSAGES.promoteFailed + detail, priority: 'urgent' });
    s.notified.promoteFailed = true;
  }
  return { state: s, actions };
}

// What the instances are told about each other on every heartbeat (for their dashboards).
export function view(state) {
  return {
    primary: { last_seen: state.primaryLastOk, role: state.primaryRole, ok: state.primaryLastOk !== null && state.primaryLastOk === state.lastTick },
    standby: { last_seen: state.standbyLastOk, role: state.standbyRole, ok: state.standbyLastOk !== null && state.standbyLastOk === state.lastTick },
  };
}
