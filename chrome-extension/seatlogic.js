// Pure seat-map decisions shared by page.js, background.js and the tests. No DOM, no network, no side effects.
(function (root) {
  'use strict';
  const FRESH_SECONDS = 600;

  // "09", " 9 " and 9 are the same seat number; anything that is not a number gives NaN.
  function num(value) {
    const text = String(value == null ? '' : value).trim();
    return /^\d{1,4}$/.test(text) ? parseInt(text, 10) : NaN;
  }
  const rowKey = (value) => String(value == null ? '' : value).trim().toUpperCase();
  const seatNo = (seat) => num(seat.displaySeatNumber != null ? seat.displaySeatNumber : seat.cinemaSeatNumber);
  const seatLabel = (seat) => rowKey(seat.rowNumber) + ' ' + seatNo(seat);

  // seatObjs: every seat on the map as {id, areaCode, rowNumber, displaySeatNumber, seatStatus, ...}.
  // pair: {category, row, seats:[a, b]} from the alert. ids come back in the pair's order.
  function findPair(seatObjs, pair) {
    const fail = (reason) => ({ ok: false, reason, ids: [] });
    if (!pair || !Array.isArray(pair.seats) || pair.seats.length !== 2 || !pair.row) return fail('This alert names no exact pair');
    const wanted = pair.seats.map(num);
    if (wanted.some(Number.isNaN) || wanted[0] === wanted[1]) return fail('The pair is not two different seat numbers');
    const row = rowKey(pair.row);
    const inRow = (Array.isArray(seatObjs) ? seatObjs : []).filter((seat) => seat && rowKey(seat.rowNumber) === row);
    if (new Set(inRow.map((seat) => seat.areaCode)).size > 1) return fail('Row label is not unique');
    const found = [];
    for (const number of wanted) {
      const matches = inRow.filter((seat) => seatNo(seat) === number);
      if (matches.length === 0) return fail('Seat ' + pair.row + ' ' + number + ' is not on the map');
      if (matches.length > 1) return fail('Seat ' + pair.row + ' ' + number + ' appears more than once');
      found.push(matches[0]);
    }
    const taken = found.filter((seat) => seat.seatStatus !== 1);
    if (taken.length) return fail(pair.row + ' ' + taken.map(seatNo).join(' + ') + ' not free');
    return { ok: true, reason: '', ids: found.map((seat) => seat.id) };
  }

  // selected: the seatObj of every "-selected" group now on the map.
  // 'exact' = exactly the two pair seats; 'partial' = nothing yet, or just one pair seat; 'different' = anything else.
  function checkSelection(selected, pair) {
    const list = Array.isArray(selected) ? selected : [];
    const row = rowKey(pair && pair.row);
    const wanted = new Set(((pair && pair.seats) || []).map(num));
    const inPair = list.filter((seat) => rowKey(seat.rowNumber) === row && wanted.has(seatNo({ displaySeatNumber: seat.cinemaSeatNumber != null ? seat.cinemaSeatNumber : seat.displaySeatNumber })));
    if (inPair.length !== list.length) return 'different';
    const distinct = new Set(inPair.map((seat) => seatNo({ displaySeatNumber: seat.cinemaSeatNumber != null ? seat.cinemaSeatNumber : seat.displaySeatNumber })));
    if (distinct.size === 2 && list.length === 2) return 'exact';
    return distinct.size === list.length && list.length <= 1 ? 'partial' : 'different';
  }

  // event.created and now are both server clock seconds (the reply's `now`).
  function isFresh(event, now) {
    const created = event && event.created;
    if (typeof created !== 'number' || typeof now !== 'number' || !isFinite(created) || !isFinite(now)) return false;
    return now - created <= FRESH_SECONDS && created - now <= 60;
  }

  function validSeatUrl(url) {
    let parsed;
    try { parsed = new URL(String(url)); } catch (error) { return false; }
    return parsed.protocol === 'https:' && parsed.hostname === 'in.bookmyshow.com' && !parsed.port && !parsed.username
      && /^\/movies\/[A-Za-z0-9-]+\/seat-layout\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_-]+)*\/?$/.test(parsed.pathname);
  }

  // Returns null when the page looks usable, else {code, message}. Only 'terms' clears by itself once the user accepts.
  function detectStop(input) {
    const state = input || {};
    const stop = (code, what) => ({ code, message: 'Stopped: ' + what + ' — take over on this page' });
    if (state.url !== undefined && !validSeatUrl(state.url)) return stop('left', 'this is no longer a seat-layout page');
    if (state.hasChallenge) return stop('challenge', 'Cloudflare check');
    const text = (String(state.title || '') + '\n' + String(state.text || '')).toLowerCase().replace(/\s+/g, ' ');
    if (/access denied|request blocked|you have been blocked|error 1020/.test(text)) return stop('denied', 'access denied');
    if (/captcha|verify (that )?you are (a )?human|are you a robot|checking your browser/.test(text)) return stop('captcha', 'a human check');
    if (/waiting room|you are in (the )?(line|queue)|you.re in (the )?(line|queue)|your turn is coming|virtual queue|queue-it/.test(text)) return stop('queue', 'a queue or waiting room');
    if (/(sign|log) ?in (to|or sign up|or register) (continue|proceed|book)|please (sign|log) ?in|login to continue|(sign|log) in required/.test(text)) return stop('signin', 'a sign-in is needed');
    if (state.hasTerms) return stop('terms', 'a terms pop-up is waiting for you');
    return null;
  }

  // On the seat map, BookMyShow labels the button that opens checkout with the seat total.
  // A bare Pay label, a different currency, or multiple matches need manual review.
  function checkoutCandidate(controls) {
    const matches = controls.filter(({ label }) =>
      /^Pay\s+(?:₹\s*|Rs\.?\s+)[0-9]+(?:,[0-9]+)*(?:\.[0-9]{1,2})?$/i.test(String(label).trim()));
    return matches.length === 1 && matches[0].enabled ? matches[0].element : null;
  }

  const api = { FRESH_SECONDS, num, findPair, checkSelection, isFresh, validSeatUrl, detectStop, seatLabel, checkoutCandidate };
  root.ScreenwatchSeatLogic = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
