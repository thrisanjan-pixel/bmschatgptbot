const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function worker(fetch, values = {}, notify = () => {}) {
  const saved = { enabled: true, since: 90, handled: [], pendingOff: [], armed: [], ...values };
  const messages = [];
  const noop = () => {};
  let opens = 0;
  const chrome = {
    storage: {
      local: { get: async defaults => structuredClone({ ...defaults, ...saved }), set: async value => Object.assign(saved, structuredClone(value)) },
      session: { set: async () => {}, get: async () => ({}), remove: async () => {} }
    },
    tabs: { create: async () => ({ id: ++opens }), update: async id => { if (id === 2) throw Error('tab failed'); }, remove: async () => {}, query: async () => [], onRemoved: { addListener: noop } },
    alarms: { onAlarm: { addListener: noop } },
    runtime: { onInstalled: { addListener: noop }, onStartup: { addListener: noop }, onMessage: { addListener: callback => messages.push(callback) } },
    notifications: { create: notify, onClicked: { addListener: noop } }, tts: { speak: noop }
  };
  const context = vm.createContext({ chrome, fetch, AbortSignal, URL, Date, setTimeout,
    self: { ScreenwatchSeatLogic: { isArmedEvent: () => true, validSeatUrl: () => true, canAutoCheckout: () => false } }, importScripts: noop });
  vm.runInContext(fs.readFileSync('chrome-extension/background.js', 'utf8'), context);
  return { context, saved, messages };
}

test('opened events persist even when a later tab fails', async () => {
  const events = ['a', 'b'].map((id) => ({ id, alert: id, created: 99, url: 'https://in.bookmyshow.com/' + id }));
  const fixture = worker(async url => ({ ok: true, json: async () => url.includes('/api/pc-alerts')
    ? { now: 100, events, armed_alerts: [{ id: 'a' }, { id: 'b' }] }
    : { alerts: events.map(event => ({ id: event.id, updated: 0, preferences: { enabled: true, open_on_pc: true } })) } }));
  await fixture.context.pollOnce();
  assert.deepEqual(fixture.saved.handled, ['a']);
  assert.deepEqual(fixture.saved.pendingOff, ['a']);
  assert.equal(fixture.saved.status.ok, false);
});

test('dashboard reads and writes carry a timeout signal', async () => {
  const calls = [];
  const fixture = worker(async (url, options) => { calls.push(options); return { ok: true, json: async () => ({ csrf: 'test' }) }; });
  await fixture.context.dashboardGet('https://example.test', '/api/watch');
  await fixture.context.dashboardAction('https://example.test', 'pc_opened', 'a');
  assert.equal(calls.length, 3);
  assert.ok(calls.every(call => call.signal instanceof AbortSignal));
});

test('notification failure does not retry an already opened tab', async () => {
  const event = { id: 'a', alert: 'a', created: 99, url: 'https://in.bookmyshow.com/a' };
  const fixture = worker(async url => ({ ok: true, json: async () => url.includes('/api/pc-alerts')
    ? { now: 100, events: [event], armed_alerts: [{ id: 'a' }] }
    : { alerts: [{ id: 'a', updated: 0, preferences: { enabled: true, open_on_pc: true } }] } }), {}, () => Promise.reject(Error('permission denied')));
  await fixture.context.pollOnce();
  assert.deepEqual(fixture.saved.handled, ['a']);
  assert.equal(fixture.saved.status.ok, true);
});
