// Run with: node --test chrome-extension/test/
const test = require('node:test');
const assert = require('node:assert');
const L = require('../seatlogic.js');

// Seats shaped like the live map: id "Seat-<area>-<n>-..." plus seatObj fields, seatStatus 1 free, 2 taken.
const seat = (row, number, extra = {}) => Object.assign({
  id: 'Seat-' + row + '-' + number, areaCode: 'A1', rowNumber: row, displaySeatNumber: String(number), cinemaSeatNumber: String(number),
  seatNumber: '0' + number, priceCode: '0001', curPrice: 400, seatType: 'Available', seatStatus: 1
}, extra);
const rowOf = (row, from, to, extra) => { const list = []; for (let n = from; n <= to; n++) list.push(seat(row, n, extra)); return list; };
const pair = { category: 'Gold', row: 'D', seats: ['8', '9'] };
const selected = (row, number) => ({ rowNumber: row, cinemaSeatNumber: String(number), priceDesc: '3D GOLD.' });

test('findPair: both free gives the ids in the pair order', () => {
  const r = L.findPair(rowOf('D', 1, 12), pair);
  assert.deepStrictEqual(r, { ok: true, reason: '', ids: ['Seat-D-8', 'Seat-D-9'] });
});
test('findPair: one seat taken', () => {
  const seats = rowOf('D', 1, 12).map((s) => (s.displaySeatNumber === '9' ? Object.assign({}, s, { seatStatus: 2, seatType: 'Booked / Held' }) : s));
  const r = L.findPair(seats, pair);
  assert.strictEqual(r.ok, false);
  assert.match(r.reason, /not free/);
  assert.deepStrictEqual(r.ids, []);
});
test('findPair: a seat missing from the map', () => {
  const r = L.findPair(rowOf('D', 1, 8), pair);
  assert.strictEqual(r.ok, false);
  assert.match(r.reason, /not on the map/);
});
test('findPair: the same row label in two areas is refused', () => {
  const seats = rowOf('D', 1, 12).concat(rowOf('D', 1, 12, { areaCode: 'A2' }));
  const r = L.findPair(seats, pair);
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.reason, 'Row label is not unique');
});
test('findPair: the same seat twice in one area is refused', () => {
  const r = L.findPair(rowOf('D', 1, 12).concat([seat('D', 8, { id: 'Seat-D-8-copy' })]), pair);
  assert.strictEqual(r.ok, false);
  assert.match(r.reason, /more than once/);
});
test('findPair: "09" and 9 are the same number, row case is ignored', () => {
  const seats = rowOf('D', 1, 12).map((s) => Object.assign({}, s, { displaySeatNumber: String(s.displaySeatNumber).padStart(2, '0') }));
  assert.strictEqual(L.findPair(seats, { category: 'Gold', row: 'd', seats: ['08', '9'] }).ok, true);
  assert.strictEqual(L.num('09'), 9);
  assert.ok(Number.isNaN(L.num('A9')));
});
test('findPair: no pair, bad pair', () => {
  assert.strictEqual(L.findPair(rowOf('D', 1, 3), null).ok, false);
  assert.strictEqual(L.findPair(rowOf('D', 1, 3), { row: 'D', seats: ['1'] }).ok, false);
  assert.strictEqual(L.findPair(rowOf('D', 1, 3), { row: 'D', seats: ['1', '1'] }).ok, false);
  assert.strictEqual(L.findPair(rowOf('D', 1, 3), { row: 'D', seats: ['x', '2'] }).ok, false);
});

test('checkSelection: exactly the two pair seats', () => {
  assert.strictEqual(L.checkSelection([selected('D', 9), selected('D', 8)], pair), 'exact');
});
test('checkSelection: one pair seat or nothing yet is partial', () => {
  assert.strictEqual(L.checkSelection([selected('D', 8)], pair), 'partial');
  assert.strictEqual(L.checkSelection([], pair), 'partial');
});
test('checkSelection: BookMyShow picked a different neighbour', () => {
  assert.strictEqual(L.checkSelection([selected('D', 8), selected('D', 7)], pair), 'different');
  assert.strictEqual(L.checkSelection([selected('E', 8), selected('E', 9)], pair), 'different');
  assert.strictEqual(L.checkSelection([selected('D', 8), selected('D', 9), selected('D', 10)], pair), 'different');
  assert.strictEqual(L.checkSelection([selected('D', 8), selected('D', 8)], pair), 'different');
});

test('isFresh: within 10 minutes of the server clock only', () => {
  assert.strictEqual(L.isFresh({ created: 1000 }, 1100), true);
  assert.strictEqual(L.isFresh({ created: 1000 }, 1600), true);
  assert.strictEqual(L.isFresh({ created: 1000 }, 1601), false);
  assert.strictEqual(L.isFresh({ created: 1100 }, 1000), false);
  assert.strictEqual(L.isFresh({}, 1000), false);
  assert.strictEqual(L.isFresh(null, 1000), false);
});

test('validSeatUrl', () => {
  const good = 'https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00523251/ALUC/6249/20261111';
  assert.strictEqual(L.validSeatUrl(good), true);
  for (const bad of [
    'http://in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/A/1/20261111',
    'https://in.bookmyshow.com.evil.example/movies/hyderabad/seat-layout/ET1/A/1/20261111',
    'https://evil.example/movies/hyderabad/seat-layout/ET1/A/1/20261111',
    'https://in.bookmyshow.com/movies/hyderabad/buytickets/ET1/20261111',
    'https://in.bookmyshow.com/explore/home/hyderabad',
    'https://user@in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/A/1/20261111',
    'javascript:alert(1)', '', null, undefined
  ]) assert.strictEqual(L.validSeatUrl(bad), false, String(bad));
});

test('detectStop: a normal seat page is fine', () => {
  const page = { url: 'https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/A/1/20261111', title: 'Select seats', text: 'How many seats? Select Seats Sign in', hasChallenge: false, hasTerms: false };
  assert.strictEqual(L.detectStop(page), null);
});
test('detectStop: each stop condition', () => {
  const ok = 'https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/A/1/20261111';
  const cases = [
    [{ url: 'https://in.bookmyshow.com/explore/home/hyderabad' }, 'left'],
    [{ url: ok, hasChallenge: true }, 'challenge'],
    [{ url: ok, title: 'Access Denied' }, 'denied'],
    [{ url: ok, text: 'Please verify you are human' }, 'captcha'],
    [{ url: ok, text: 'Complete the CAPTCHA to continue' }, 'captcha'],
    [{ url: ok, text: 'You are in the queue. Your turn is coming' }, 'queue'],
    [{ url: ok, text: 'Welcome to the waiting room' }, 'queue'],
    [{ url: ok, text: 'Sign in to continue booking' }, 'signin'],
    [{ url: ok, text: 'Please log in' }, 'signin'],
    [{ url: ok, text: 'Terms and conditions', hasTerms: true }, 'terms']
  ];
  for (const [input, code] of cases) {
    const stop = L.detectStop(input);
    assert.ok(stop, code);
    assert.strictEqual(stop.code, code);
    assert.match(stop.message, /^Stopped: .+ — take over on this page$/);
  }
});

test('checkoutCandidate requires one enabled seat-total button', () => {
  const enabled = { element: {}, label: 'Pay ₹1,280.00', enabled: true };
  assert.strictEqual(L.checkoutCandidate([enabled]), enabled.element);
  assert.strictEqual(L.checkoutCandidate([{ ...enabled, label: 'Pay Rs. 800' }]), enabled.element);
  assert.strictEqual(L.checkoutCandidate([{ ...enabled, enabled: false }]), null);
  assert.strictEqual(L.checkoutCandidate([enabled, { ...enabled, enabled: false }]), null);
  for (const label of ['Pay', 'Pay now', 'Pay ₹800 using saved card', 'Pay $800', 'Accept and continue']) {
    assert.strictEqual(L.checkoutCandidate([{ ...enabled, label }]), null);
  }
});

test('automatic checkout requires a fresh exact pair after local opt-in for this arming', () => {
  const dashboard = 'https://screenwatch.example';
  const alert = { id: 'a1', until: 1200 };
  const grant = { dashboard, until: 1200, armedAt: 1040000 };
  const event = { alert: 'a1', created: 1041, pair };
  const eligible = (g, e = event, a = alert) => L.canAutoCheckout(g, dashboard, a, e, 1050, 1050000);
  assert.strictEqual(eligible(grant), true);
  assert.strictEqual(eligible(null), false);
  assert.strictEqual(eligible(grant, { ...event, created: 1039 }), false);
  assert.strictEqual(eligible(grant, { ...event, alert: 'a2' }), false);
  assert.strictEqual(eligible(grant, event, { ...alert, until: 1300 }), false);
  assert.strictEqual(eligible(grant, { ...event, pair: { ...pair, seats: ['8', '10'] } }), false);
});
