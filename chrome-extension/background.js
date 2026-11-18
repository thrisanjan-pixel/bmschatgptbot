// Service worker. It only talks to the Screenwatch dashboard (never to BookMyShow, never with cookies or page data of BookMyShow).
// While switched on it polls /api/pc-alerts, opens the seat page of a fresh alert in a new tab and tells the dashboard it did.
importScripts('seatlogic.js');
const L = self.ScreenwatchSeatLogic;
const DEFAULT_URL = 'https://bmschatgptbot-production.up.railway.app';
const ALARM = 'screenwatch-poll';
const FAST = 3000, LOOP_MS = 27000, HANDLED_KEEP = 200, TASK_SECONDS = 900;
let looping = false, csrfToken = '';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const local = { get: (keys) => chrome.storage.local.get(keys), set: (values) => chrome.storage.local.set(values) };
async function config() {
  const c = await local.get({ enabled: false, autoAcceptTerms: false, dashboard: DEFAULT_URL, since: null, handled: [], pendingOff: [], armed: [] });
  c.dashboard = String(c.dashboard || DEFAULT_URL).replace(/\/+$/, '');
  return c;
}
async function setStatus(ok, text) { await local.set({ status: { ok, text, at: Date.now() } }); }

async function dashboardGet(base, path, signal) {
  signal ||= AbortSignal.timeout(10000);
  const response = await fetch(base + path, { credentials: 'include', cache: 'no-store', redirect: 'manual', signal });
  if (response.type === 'opaqueredirect' || response.status === 401 || response.status === 403) throw new Error('Sign in to the dashboard in this Chrome first');
  if (!response.ok) throw new Error('Dashboard answered ' + response.status);
  try { return await response.json(); } catch (error) { throw new Error('Sign in to the dashboard in this Chrome first'); }
}

// The CSRF token comes from the page's own JSON, then every POST carries it.
async function dashboardAction(base, action, alert) {
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!csrfToken) csrfToken = (await dashboardGet(base, '/api/watch?new=1')).csrf || '';
    const response = await fetch(base + '/api/watch', {
      method: 'POST', credentials: 'include', cache: 'no-store',
      signal: AbortSignal.timeout(10000),
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ action, alert })
    });
    if (response.status === 403 && attempt === 0) { csrfToken = ''; continue; }
    if (!response.ok) throw new Error('Dashboard answered ' + response.status);
    return true;
  }
  return false;
}

async function flushPendingOff(c) {
  const left = [];
  for (const alert of c.pendingOff) {
    try { await dashboardAction(c.dashboard, 'pc_opened', alert); } catch (error) { left.push(alert); }
  }
  if (left.length !== c.pendingOff.length) await local.set({ pendingOff: left });
  c.pendingOff = left;
}

async function openEvent(c, event, serverNow, autoCheckout) {
  const tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  // Save the task before navigation so even a cached seat page can claim it on its first load.
  await chrome.storage.session.set({ ['task:' + tab.id]: { event, autoCheckout, skew: Date.now() / 1000 - serverNow } });
  const show = event.show || {};
  const next = autoCheckout ? 'Auto checkout is on for this tab.' : event.partySize === 0
    ? 'Set Minimum seats open to enable automatic selection for future alerts.' : 'Choose seats on BookMyShow.';
  await local.set({ last: { text: 'Opening ' + (show.movie || event.summary || 'a seat page') + '. ' + next, at: Date.now() } });
  try {
    await chrome.tabs.update(tab.id, { url: event.url });
  } catch (error) {
    await chrome.tabs.remove(tab.id).catch(() => {});
    throw error;
  }
  if (tab.windowId !== undefined) chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
  // Desktop notification permission must not make a successfully opened tab retry.
  Promise.resolve(chrome.notifications.create('screenwatch-' + event.id, {
    type: 'basic', iconUrl: 'icon128.png', requireInteraction: true, title: 'Seats open',
    message: [show.movie, show.venue, event.summary].filter(Boolean).join(' · ') || 'A Screenwatch alert fired. Check Chrome.'
  })).catch(() => {});
  chrome.tts.speak('Seats open. Check Chrome.');
}

async function pollOnce() {
  const c = await config();
  if (!c.enabled) return { armed: false };
  try {
    await flushPendingOff(c);
    const first = c.since === null;
    const data = await dashboardGet(c.dashboard, '/api/pc-alerts?since=' + encodeURIComponent(first ? 9e12 : c.since) + '&client=extension');
    const handled = c.handled.slice();
    const seenAlerts = new Set();
    let watch;
    for (const event of data.events || []) {
      if (!event || handled.includes(event.id)) continue;
      handled.push(event.id);
      if (seenAlerts.has(event.alert)) continue;
      const alert = (data.armed_alerts || []).find((a) => a.id === event.alert);
      if (!alert || !L.isArmedEvent(alert, event, data.now) || !L.validSeatUrl(event.url)) continue;
      watch ??= await dashboardGet(c.dashboard, '/api/watch?new=1');
      const rule = watch.alerts?.find((rule) => rule.id === event.alert);
      const preferences = rule?.preferences;
      if (!preferences?.open_on_pc) continue;
      // A one-shot alert can complete when Telegram delivers it, before Chrome sees it.
      const justCompleted = preferences.once && !preferences.request_state && rule.status === 'completed'
        && rule.completed >= event.created && rule.history?.some((item) => item.created === event.created && item.url === event.url);
      if (!justCompleted && (!preferences.enabled || rule.updated > event.created)) continue;
      event.partySize = preferences.min_seats || (preferences.row_show ? preferences.party_size : 0) || 0;
      event.allowedRows = preferences.row_show ? (preferences.rows || []) : null;
      event.allowedCategories = preferences.categories || [];
      seenAlerts.add(event.alert);
      const autoCheckout = L.canAutoCheckout(event);
      if (autoCheckout) {
        const [movie, venue, session, date] = new URL(event.url).pathname.split('/').slice(4);
        const showId = [venue, date, session, movie].join(':');
        // The saved map supplies category names only. The page supplies live availability.
        const layout = await dashboardGet(c.dashboard, '/api/rows?show=' + encodeURIComponent(showId), AbortSignal.timeout(2000)).catch(() => null);
        if (Array.isArray(layout?.data?.rows)) event.layoutRows = layout.data.rows;
      }
      await openEvent(c, event, data.now, autoCheckout);
      if (!c.pendingOff.includes(event.alert)) c.pendingOff.push(event.alert);
      // Persist each opened tab before the next network operation can fail.
      await local.set({ handled: handled.slice(-HANDLED_KEEP), pendingOff: c.pendingOff });
    }
    await local.set({ since: data.now - 5, handled: handled.slice(-HANDLED_KEEP), pendingOff: c.pendingOff, armed: data.armed_alerts || [] });
    await flushPendingOff(c);
    await setStatus(true, 'Connected to the dashboard');
    await cleanTasks();
    return { armed: (data.armed_alerts || []).length > 0 || c.pendingOff.length > 0 };
  } catch (error) {
    await setStatus(false, String(error && error.message || error));
    return { armed: false };
  }
}

async function loop() {
  if (looping) return;
  looping = true;
  try {
    const end = Date.now() + LOOP_MS;
    for (;;) {
      const { armed } = await pollOnce();
      if (!armed || Date.now() + FAST >= end) break;
      await sleep(FAST);
    }
  } finally { looping = false; }
}

async function cleanTasks() {
  const all = await chrome.storage.session.get(null);
  const tabs = new Set((await chrome.tabs.query({})).map((t) => String(t.id)));
  const drop = Object.keys(all).filter((k) => k.startsWith('task:') && (!tabs.has(k.slice(5)) || expired(all[k])));
  if (drop.length) await chrome.storage.session.remove(drop);
}
const expired = (task) => !task || !task.event || Date.now() / 1000 - task.skew - task.event.created > TASK_SECONDS;

async function sync() {
  const c = await config();
  if (c.enabled) { await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 }); loop().catch(reportError); }
  else { await chrome.alarms.clear(ALARM); await local.set({ armed: [], status: { ok: false, text: 'Off', at: Date.now() } }); }
}

function reportError(error) { setStatus(false, String(error?.message || error)).catch(() => {}); }
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === ALARM) loop().catch(reportError); });
chrome.runtime.onInstalled.addListener(() => sync().catch(reportError));
chrome.runtime.onStartup.addListener(() => sync().catch(reportError));
chrome.tabs.onRemoved.addListener((tabId) => { chrome.storage.session.remove('task:' + tabId).catch(() => {}); });
chrome.notifications.onClicked.addListener((id) => { chrome.notifications.clear(id).catch(reportError); });

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
    if (!message || typeof message.type !== 'string') return reply({ ok: false, error: 'Invalid message' });
    if (message.type === 'get-task') {
      // Only a tab this extension opened, only for the show the alert named, and only for 15 minutes.
      const tabId = sender.tab && sender.tab.id;
      if (tabId === undefined) return reply({ task: null });
      const key = 'task:' + tabId, task = (await chrome.storage.session.get(key))[key];
      let samePage = false;
      try { samePage = !!task && new URL(sender.url || '').pathname === new URL(task.event.url).pathname; } catch (error) { samePage = false; }
      if (!task || expired(task) || !samePage) { if (task && expired(task)) await chrome.storage.session.remove(key); return reply({ task: null }); }
      if (task.autoCheckout) {
        const c = await config();
        await chrome.storage.session.set({ [key]: { ...task, autoCheckout: false } });
        task.autoCheckout = c.enabled;
        task.autoAcceptTerms = c.enabled && c.autoAcceptTerms === true;
      }
      return reply({ task });
    }
    if (message.type === 'result') {
      await local.set({ last: { text: String(message.text || ''), ok: !!message.ok, at: Date.now() } });
      return reply({ ok: true });
    }
    if (message.type === 'dismiss') {
      if (sender.tab) await chrome.storage.session.remove('task:' + sender.tab.id);
      await local.set({ last: { text: 'Dismissed the seat banner.', at: Date.now() } });
      return reply({ ok: true });
    }
    if (message.type === 'cancel') {
      const c = await config();
      try { await dashboardAction(c.dashboard, 'pc_cancel', String(message.alert || '')); } catch (error) { return reply({ ok: false, error: String(error.message || error) }); }
      await local.set({ armed: c.armed.filter((a) => a.id !== message.alert) });
      return reply({ ok: true });
    }
    if (message.type === 'sync') { csrfToken = ''; await sync(); return reply({ ok: true }); }
    reply({});
  })().catch((error) => { reportError(error); reply({ ok: false, error: String(error?.message || error) }); });
  return true;
});
