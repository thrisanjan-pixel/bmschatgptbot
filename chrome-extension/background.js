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
  const c = await local.get({ enabled: false, dashboard: DEFAULT_URL, since: null, handled: [], pendingOff: [], armed: [] });
  c.dashboard = String(c.dashboard || DEFAULT_URL).replace(/\/+$/, '');
  return c;
}
async function setStatus(ok, text) { await local.set({ status: { ok, text, at: Date.now() } }); }

async function dashboardGet(base, path) {
  const response = await fetch(base + path, { credentials: 'include', cache: 'no-store', redirect: 'manual' });
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
  const tab = await chrome.tabs.create({ url: event.url, active: true });
  if (tab.windowId !== undefined) chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
  await chrome.storage.session.set({ ['task:' + tab.id]: { event, autoCheckout, skew: Date.now() / 1000 - serverNow } });
  const show = event.show || {};
  chrome.notifications.create('screenwatch-' + event.id, {
    type: 'basic', iconUrl: 'icon128.png', requireInteraction: true, title: 'Seats open',
    message: [show.movie, show.venue, event.summary].filter(Boolean).join(' · ') || 'A Screenwatch alert fired. Check Chrome.'
  });
  chrome.tts.speak('Seats open. Check Chrome.');
  await local.set({ last: { text: 'Opened ' + (show.movie || event.summary || 'a seat page') + '. Waiting for you on the page.', at: Date.now() } });
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
    for (const event of data.events || []) {
      if (!event || handled.includes(event.id)) continue;
      handled.push(event.id);
      if (seenAlerts.has(event.alert)) continue;
      if (!L.isFresh(event, data.now) || !L.validSeatUrl(event.url)) continue;
      const alert = (data.armed_alerts || []).find((a) => a.id === event.alert);
      if (!alert) continue;
      seenAlerts.add(event.alert);
      const key = 'auto:' + alert.id;
      const grant = (await chrome.storage.session.get(key))[key];
      const autoCheckout = L.canAutoCheckout(grant, c.dashboard, alert, event, data.now, Date.now());
      if (autoCheckout) await chrome.storage.session.remove(key);
      await openEvent(c, event, data.now, autoCheckout);
      if (!c.pendingOff.includes(event.alert)) c.pendingOff.push(event.alert);
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
  if (c.enabled) { await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 }); loop(); }
  else { await chrome.alarms.clear(ALARM); await local.set({ armed: [], status: { ok: false, text: 'Off', at: Date.now() } }); }
}

chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === ALARM) loop(); });
chrome.runtime.onInstalled.addListener(sync);
chrome.runtime.onStartup.addListener(sync);
chrome.tabs.onRemoved.addListener((tabId) => { chrome.storage.session.remove('task:' + tabId).catch(() => {}); });
chrome.notifications.onClicked.addListener((id) => { chrome.notifications.clear(id); });

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  (async () => {
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
      await chrome.storage.session.remove('auto:' + String(message.alert || ''));
      try { await dashboardAction(c.dashboard, 'pc_cancel', String(message.alert || '')); } catch (error) { return reply({ ok: false, error: String(error.message || error) }); }
      await local.set({ armed: c.armed.filter((a) => a.id !== message.alert) });
      return reply({ ok: true });
    }
    if (message.type === 'sync') { csrfToken = ''; await sync(); return reply({ ok: true }); }
    reply({});
  })();
  return true;
});
