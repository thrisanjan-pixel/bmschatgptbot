// Pure seat-map decisions shared by page.js, background.js and the tests. No DOM, no network, no side effects.
(function (root) {
  'use strict';
  const FRESH_SECONDS = 600;
  const OPEN_ON_PC_SECONDS = 3 * 3600; // Matches PC_ARM in watch_preferences.py.

  // "09", " 9 " and 9 are the same seat number; anything that is not a number gives NaN.
  function num(value) {
    const text = String(value == null ? '' : value).trim();
    return /^\d{1,4}$/.test(text) ? parseInt(text, 10) : NaN;
  }
  const rowKey = (value) => String(value == null ? '' : value).trim().replace(/\s+/g, ' ').toUpperCase();
  const seatNo = (seat) => num(seat.displaySeatNumber != null ? seat.displaySeatNumber : seat.cinemaSeatNumber);
  const seatLabel = (seat) => rowKey(seat.rowNumber) + ' ' + seatNo(seat);
  const rowId = (category, row) => rowKey(category) + ' / ' + rowKey(row);

  function categoriesByRow(layoutRows) {
    const categories = new Map();
    for (const entry of layoutRows) {
      const row = rowKey(entry.row), category = rowKey(entry.category);
      if (!row || !category) continue;
      if (!categories.has(row)) categories.set(row, category);
      else if (categories.get(row) !== category) categories.set(row, null);
    }
    return categories;
  }

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

  function rankBlocks(seats, size, preferred, allowedRows, allowedCategories = []) {
    const allowed = allowedRows === null ? null : new Set((allowedRows ?? (preferred ? [rowId(preferred.category, preferred.row)] : [])).map(rowKey));
    const categories = new Set(allowedCategories.map(rowKey));
    const rows = new Map();
    for (const seat of seats) {
      const id = rowId(seat.priceDesc, seat.rowNumber);
      const category = rowKey(seat.priceDesc);
      if (!category || !rowKey(seat.rowNumber) || !Number.isFinite(seat.x)) continue;
      if (!rows.has(id)) rows.set(id, []);
      rows.get(id).push(seat);
    }
    const geometry = [...rows.values()].map((row) => {
      row.sort((a, b) => a.x - b.x);
      return { span: row.at(-1).x - row[0].x, centre: (row.at(-1).x + row[0].x) / 2, y: row[0].y };
    });
    if (!geometry.length) return [];
    const widest = Math.max(...geometry.map((row) => row.span));
    const centres = geometry.filter((row) => row.span >= widest * 0.85).map((row) => row.centre).sort((a, b) => a - b);
    const centreX = (centres[Math.floor((centres.length - 1) / 2)] + centres[Math.floor(centres.length / 2)]) / 2;
    const depths = geometry.map((row) => row.y).filter(Number.isFinite);
    const firstY = depths.length ? Math.min(...depths) : 0;
    const lastY = depths.length ? Math.max(...depths) : 0;
    const centreY = (firstY + lastY) / 2;
    const halfDepth = (lastY - firstY) / 2;
    const ranked = [];
    for (const [id, row] of rows) {
      if ((allowed && !allowed.has(id)) || (categories.size && !categories.has(rowKey(row[0].priceDesc)))) continue;
      const numbers = row.map(seatNo).filter(Number.isFinite);
      if (new Set(numbers).size !== numbers.length) continue;
      for (let i = 0; i + size <= row.length; i++) {
        const block = row.slice(i, i + size);
        if (!block.every((seat, index) => {
          if (![1, 4, '1', '4'].includes(seat.seatStatus) || !Number.isFinite(seatNo(seat))) return false;
          if (!index) return true;
          const previous = block[index - 1], gap = seat.x - previous.x;
          return seat.areaCode === previous.areaCode && Math.abs(seatNo(seat) - seatNo(previous)) === 1
            && gap > 0 && gap <= 1.65 * Math.max(seat.width, previous.width);
        })) continue;
        const a = block[0], b = block.at(-1);
        const centre = Math.abs((a.x + b.x) / 2 - centreX) / (widest / 2 || 1);
        const depth = halfDepth ? (Number.isFinite(a.y) ? Math.abs(a.y - centreY) / halfDepth : 1) : 0;
        ranked.push({
          pair: { category: a.priceDesc, row: a.rowNumber, seats: block.map(seatNo) },
          ids: block.map((seat) => seat.id),
          // Make the outermost rows cost more while allowing an excellent centre block
          // to beat a block near the side of the auditorium. Normalize for map zoom.
          score: centre + depth ** 2,
          depth,
          centre
        });
      }
    }
    const exact = (candidate) => preferred?.seats.length === size && rowId(candidate.pair.category, candidate.pair.row) === rowId(preferred.category, preferred.row)
      && preferred.seats.every((seat) => candidate.pair.seats.includes(num(seat)));
    return ranked.sort((a, b) => a.score - b.score || a.depth - b.depth || a.centre - b.centre || Number(exact(b)) - Number(exact(a)));
  }

  // selected: the seatObj of every "-selected" group now on the map.
  // 'exact' = exactly the two pair seats; 'partial' = nothing yet, or just one pair seat; 'different' = anything else.
  function checkSelection(selected, pair) {
    const list = Array.isArray(selected) ? selected : [];
    const row = rowKey(pair && pair.row);
    const wanted = new Set(((pair && pair.seats) || []).map(num));
    const inPair = list.filter((seat) => rowKey(seat.rowNumber) === row && wanted.has(seatNo(seat)));
    if (inPair.length !== list.length) return 'different';
    const distinct = new Set(inPair.map(seatNo));
    if (distinct.size !== list.length || !wanted.size) return 'different';
    return distinct.size === wanted.size ? 'exact' : 'partial';
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

  function isArmedEvent(alert, event, serverNow) {
    return alert.id === event.alert && alert.until > serverNow
      && isFresh(event, serverNow)
      && event.created >= alert.until - OPEN_ON_PC_SECONDS;
  }

  function canAutoCheckout(event) {
    if (event.partySize !== undefined) return Number.isInteger(event.partySize) && event.partySize >= 1 && event.partySize <= 10
      && (event.allowedRows === null || (Array.isArray(event.allowedRows) && event.allowedRows.length > 0));
    const seats = event.pair?.seats;
    return typeof event.pair?.row === 'string' && !!event.pair.row.trim()
      && typeof event.pair.category === 'string' && !!event.pair.category.trim()
      && Array.isArray(seats) && seats.length === 2 && Math.abs(num(seats[0]) - num(seats[1])) === 1;
  }

  const api = { FRESH_SECONDS, num, findPair, categoriesByRow, rankBlocks, checkSelection, isFresh, validSeatUrl, detectStop, seatLabel, checkoutCandidate, isArmedEvent, canAutoCheckout };
  root.ScreenwatchSeatLogic = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
