import { test } from 'node:test';
import assert from 'node:assert/strict';
import { MESSAGES, afterPromote, decide, initialState } from '../src/decide.js';

const up = (role) => ({ ok: true, role });
const down = { ok: false };
const cfg = { graceSeconds: 180 };

function run(steps, start = 1000) {
  let state = initialState(start);
  const log = [];
  for (const [offset, primary, standby] of steps) {
    const result = decide(state, { now: start + offset, primary, standby }, cfg);
    state = result.state;
    log.push(result.actions);
  }
  return { state, log };
}

test('healthy pair does nothing', () => {
  const { log } = run([[0, up('active'), up('standby')], [60, up('active'), up('standby')]]);
  assert.deepEqual(log.flat(), []);
});

test('primary missed for less than the grace period is not promoted', () => {
  const { log } = run([[0, up('active'), up('standby')], [60, down, up('standby')], [180, down, up('standby')]]);
  assert.deepEqual(log.flat(), []);
});

test('primary missed beyond the grace period promotes the standby', () => {
  const { log } = run([[0, up('active'), up('standby')], [120, down, up('standby')], [181, down, up('standby')]]);
  assert.deepEqual(log[2], [{ type: 'promote' }]);
});

test('promotion retries each tick until the standby reports active, then stops', () => {
  const { log } = run([[0, up('active'), up('standby')], [200, down, up('standby')], [260, down, up('standby')], [320, down, up('active')]]);
  assert.deepEqual(log[1], [{ type: 'promote' }]);
  assert.deepEqual(log[2], [{ type: 'promote' }]);
  assert.deepEqual(log[3], []);
});

test('a primary that never answered counts from watchdog start', () => {
  const { log } = run([[0, down, up('standby')], [181, down, up('standby')]]);
  assert.deepEqual(log[0], []);
  assert.deepEqual(log[1], [{ type: 'promote' }]);
});

test('promote success notifies once; failure notifies once per episode', () => {
  let state = initialState(0);
  let r = afterPromote(state, { ok: true, role: 'active' });
  assert.equal(r.actions[0].message, MESSAGES.promoted);
  r = afterPromote(r.state, { ok: true, role: 'active' });
  assert.deepEqual(r.actions, []);
  state = initialState(0);
  r = afterPromote(state, { ok: false, status: 503 });
  assert.match(r.actions[0].message, /FAILED.*503/);
  assert.deepEqual(afterPromote(r.state, { ok: false, status: 503 }).actions, []);
});

test('both instances down alerts once per episode and never promotes', () => {
  const { log, state } = run([[0, up('active'), up('standby')], [100, down, down], [181, down, down], [240, down, down]]);
  assert.deepEqual(log[1], []);
  assert.deepEqual(log[2], [{ type: 'notify', key: 'bothDown', message: MESSAGES.bothDown, priority: 'urgent' }]);
  assert.deepEqual(log[3], []);
  assert.equal(state.notified.bothDown, true);
  const again = decide(state, { now: 1300, primary: down, standby: up('standby') }, cfg);
  assert.equal(again.state.notified.bothDown, false);
});

test('no automatic failback when the primary returns', () => {
  const { log } = run([[0, down, up('active')], [300, down, up('active')], [360, up('standby'), up('active')]]);
  assert.deepEqual(log.flat(), []);
});

test('both reporting active warns once per episode', () => {
  const { log } = run([[0, up('active'), up('active')], [60, up('active'), up('active')], [120, up('active'), up('standby')], [180, up('active'), up('active')]]);
  assert.equal(log[0][0].key, 'bothActive');
  assert.deepEqual(log[1], []);
  assert.deepEqual(log[2], []);
  assert.equal(log[3][0].key, 'bothActive');
});

test('grace period comes from config', () => {
  const state = initialState(0);
  const r = decide({ ...state, primaryLastOk: 0 }, { now: 61, primary: down, standby: up('standby') }, { graceSeconds: 60 });
  assert.deepEqual(r.actions, [{ type: 'promote' }]);
});
