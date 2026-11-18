const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('a failed queue refresh stays visibly stale through timer renders', async () => {
  const nodes = new Map();
  let signal;
  const context = vm.createContext({ Date, AbortSignal,
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    fetch: async (url, options) => { signal = options.signal; throw Error('offline'); }
  });
  const html = fs.readFileSync('static/screenwatch.html', 'utf8');
  vm.runInContext(html.slice(html.indexOf('let checkQueue=null'), html.indexOf('let staleSaving=false')), context);
  vm.runInContext('checkQueue={now:1};cqBuilt=checkQueue;', context);
  await context.refreshCheckQueue();
  context.renderCheckQueue();
  assert.equal(nodes.get('cq-updated').textContent, 'Connection lost · retrying');
  assert.ok(signal instanceof AbortSignal);
});

test('blocked checks and discarded dates have clear non-error dashboard text', () => {
  const html = fs.readFileSync('static/screenwatch.html', 'utf8');
  const context = vm.createContext({});
  vm.runInContext(html.split('\n').find(line => line.startsWith('function plainError(')), context);
  assert.equal(context.plainError('Source HTTP 403'), 'BMS blocked one check · retrying');
  assert.equal(context.plainError('Screening event/date mismatch'), 'Waiting for requested date · discarded another date');
  vm.runInContext(html.split('\n').find(line => line.startsWith('function alertHealthLabel(')), context);
  assert.equal(context.alertHealthLabel({ error: 'Source HTTP 403', label: 'BMS blocked one check' }), 'BMS blocked one check');
  assert.equal(context.alertHealthLabel({ error: 'Screening event/date mismatch', label: 'Waiting for requested date' }), 'Waiting for requested date');
  assert.equal(context.alertHealthLabel({ error: 'Connection error', label: 'Source unavailable' }), 'Fresh data unavailable');
  vm.runInContext(html.split('\n').find(line => line.startsWith('function rowError(')), context);
  assert.equal(context.rowError('Seat source HTTP 403'), 'BMS blocked one check. No seat data was returned.');
});

test('reliability dashboard retains blocked counts without labelling them HTTP errors', () => {
  const nodes = new Map();
  const context = vm.createContext({ Date,
    $: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    stamp: String, age: String, duration: String
  });
  const html = fs.readFileSync('static/screenwatch.html', 'utf8');
  vm.runInContext(html.split('\n').find(line => line.startsWith('function renderReliability(')), context);
  context.renderReliability({ requests: [{ at: 1, host: 'in.bookmyshow.com', status: 403 }],
    checks: [{ attempts: 2, successes: 0 }], blocked_checks: { count: 2 }, health_heartbeat: Date.now()/1000 });
  assert.match(nodes.get('source-request-log').textContent, /BMS blocked one check/);
  assert.doesNotMatch(nodes.get('source-request-log').textContent, /HTTP 403/);
  assert.match(nodes.get('check-performance').textContent, /2 total blocked checks/);
});
