// Runs in the page's own (MAIN) world so it can reach the seat map's Konva stage. It does nothing until content.js sends a
// token and a pair for a tab the extension opened. It reports when the exact pair has been selected.
(function () {
  'use strict';
  if (window.__screenwatchPage) return;
  window.__screenwatchPage = true;
  const L = globalThis.ScreenwatchSeatLogic;
  // Test hook: only on a file: page that set the flag itself (test/fixture.html). Never active on a website.
  const TEST = window.__SCREENWATCH_TEST__ === true && location.protocol === 'file:';
  let token = '', pair = null, started = false, finished = false, busy = false, latched = false;
  let lastKey = '', lastVerify = 0, ids = [], overlay = null, rings = [];

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const stage = () => (window.Konva && window.Konva.stages && window.Konva.stages[0]) || null;
  const visible = (node) => !!node && (node.offsetParent !== null || (node.getClientRects && node.getClientRects().length > 0));

  function post(type, data) {
    const message = Object.assign({ src: 'screenwatch-page', token, type }, data || {});
    if (TEST) (window.__swLog = window.__swLog || []).push(message);
    window.postMessage(message, TEST ? '*' : location.origin);
  }
  function state(phase, text, canSelect) {
    const key = phase + '|' + text + '|' + !!canSelect;
    if (key === lastKey) return;
    lastKey = key;
    post('state', { phase, text, canSelect: !!canSelect });
  }

  const allGroups = () => { const s = stage(); return s ? s.find('Group').filter((g) => g.attrs && g.attrs.seatObj && /^Seat-/.test(String(g.attrs.id || ''))) : []; };
  const isSelected = (g) => /-selected$/.test(String(g.attrs.id));
  const seatObjs = () => allGroups().filter((g) => !isSelected(g)).map((g) => Object.assign({ id: g.attrs.id }, g.attrs.seatObj));
  const selectedGroups = () => allGroups().filter(isSelected);
  const groupById = (id) => allGroups().find((g) => g.attrs.id === id && !isSelected(g));

  function screenRect(group) {
    const s = stage(), r = group.getClientRect(), b = s.content.getBoundingClientRect();
    const scale = (s.width && s.width()) ? b.width / s.width() : 1;
    return { x: b.left + r.x * scale, y: b.top + r.y * scale, w: r.width * scale, h: r.height * scale };
  }
  function clickSeat(group) {
    const r = screenRect(group), x = r.x + r.w / 2, y = r.y + r.h / 2;
    group.findOne('Rect').fire('click', { evt: new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }) }, true);
  }

  // Page conditions for detectStop. Reads text and the DOM only.
  function termsWaiting() {
    const buttons = Array.from(document.querySelectorAll('button,[role=button],a')).filter((b) => visible(b) && /^(accept|i accept|i agree|agree|accept (&|and) continue)$/i.test((b.textContent || '').trim()));
    return buttons.some((b) => { let n = b; for (let i = 0; i < 6 && n; i++, n = n.parentElement) if (/terms/i.test((n.textContent || '').slice(0, 4000)) && n !== document.body) return true; return false; });
  }
  function stopNow() {
    return L.detectStop({
      url: TEST ? undefined : location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 30000),
      hasChallenge: !!document.querySelector('iframe[src*="challenges.cloudflare.com"],#challenge-form'), hasTerms: termsWaiting()
    });
  }

  function quantityPopup() {
    const button = Array.from(document.querySelectorAll('button')).find((b) => visible(b) && /^select seats$/i.test((b.textContent || '').trim()));
    const items = Array.from(document.querySelectorAll('li')).filter((li) => visible(li) && /^[1-6]$/.test((li.textContent || '').trim()));
    return button && items.length >= 2 ? { button, items } : null;
  }

  // Highlight rings: fixed-position boxes that follow the seats; they never take clicks.
  function drawRings() {
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483646;pointer-events:none';
      document.documentElement.appendChild(overlay);
    }
    if (!ids.length) { overlay.replaceChildren(); rings = []; return; }
    if (rings.length !== ids.length) {
      overlay.replaceChildren();
      rings = ids.map(() => { const d = document.createElement('div'); d.style.cssText = 'position:fixed;border:3px solid #ff2d95;border-radius:8px;box-shadow:0 0 0 2px #fff,0 0 12px 4px #ff2d95;box-sizing:border-box'; overlay.appendChild(d); return d; });
    }
    ids.forEach((id, i) => {
      const g = groupById(id);
      if (!g) { rings[i].style.display = 'none'; return; }
      const r = screenRect(g);
      rings[i].style.cssText += ';display:block;left:' + (r.x - 4) + 'px;top:' + (r.y - 4) + 'px;width:' + (r.w + 8) + 'px;height:' + (r.h + 8) + 'px';
    });
  }
  function clearRings() { ids = []; if (overlay) { overlay.remove(); overlay = null; rings = []; } }

  function verify() {
    lastVerify = Date.now();
    const result = L.findPair(seatObjs(), pair);
    if (result.ok) { ids = result.ids; state('verified', 'Pair verified: ' + pair.row + ' ' + pair.seats.join(' + ') + ' (' + pair.category + ') are free.', true); }
    else {
      ids = [];
      if (result.reason === 'Row label is not unique') state('stopped', 'Stopped: Row label is not unique — take over on this page', false);
      else state('gone', 'Pair gone: ' + pair.row + ' ' + pair.seats.join(' + ') + ' are no longer both free. Nothing was selected. (' + result.reason + ')', false);
    }
    return result;
  }

  function finish(text, ok) { finished = true; clearInterval(timer); clearRings(); state(ok ? 'done' : 'error', text, false); post('result', { text, ok: !!ok }); }
  const list = (groups) => groups.map((g) => L.seatLabel(Object.assign({}, g.attrs.seatObj, { displaySeatNumber: g.attrs.seatObj.cinemaSeatNumber != null ? g.attrs.seatObj.cinemaSeatNumber : g.attrs.seatObj.displaySeatNumber }))).join(', ') || 'none';

  async function waitFor(test, ms, step) {
    const end = Date.now() + ms;
    while (Date.now() < end) { if (test()) return true; await sleep(step || 100); }
    return test();
  }

  async function select() {
    if (busy || finished || latched || (!TEST && !navigator.userActivation.isActive)) return;
    busy = true;
    try {
      const stop = stopNow();
      if (stop) { latched = stop.code !== 'terms'; state('stopped', stop.message, false); return; }
      const first = verify();
      if (!first.ok) return;
      if (selectedGroups().length) return finish('You already have seats selected. Clear them first.', false);
      state('selecting', 'Selecting…', false);
      const popup = quantityPopup();
      if (popup) {
        const two = popup.items.find((li) => (li.textContent || '').trim() === '2');
        two.click();
        await sleep(150);
        popup.button.click();
        if (!(await waitFor(() => !quantityPopup(), 5000))) return finish('The seat-count pop-up did not close. Take over on this page.', false);
        await sleep(200);
      }
      const again = verify();
      if (!again.ok) { state('gone', 'Pair gone: ' + pair.row + ' ' + pair.seats.join(' + ') + ' are no longer both free. Nothing was selected.', false); return; }
      const firstGroup = groupById(again.ids[0]);
      if (!firstGroup) return finish('Seat ' + pair.row + ' ' + pair.seats[0] + ' disappeared. Take over on this page.', false);
      clickSeat(firstGroup);
      await waitFor(() => selectedGroups().length >= 2, 2000);
      let check = L.checkSelection(selectedGroups().map((g) => g.attrs.seatObj), pair);
      if (check === 'partial' && selectedGroups().length === 1) {
        const second = groupById(again.ids[1]);
        if (second) { clickSeat(second); await waitFor(() => selectedGroups().length >= 2, 2000); }
        check = L.checkSelection(selectedGroups().map((g) => g.attrs.seatObj), pair);
      }
      const now = selectedGroups();
      if (check === 'exact') {
        const category = String(pair.category || '').trim();
        if (!category || now.some((g) => String(g.attrs.seatObj.priceDesc || '').trim().toUpperCase() !== category.toUpperCase())) {
          return finish('Both seats were selected, but their section could not be verified. Review and continue on BookMyShow.', false);
        }
        const afterSelection = stopNow();
        if (afterSelection) return finish(afterSelection.message, false);
        finished = true;
        clearInterval(timer);
        clearRings();
        post('checkout-ready', { text: 'Selected ' + pair.row + ' ' + pair.seats.join(' + ') + ' (' + category + ').' });
        return;
      }
      if (!now.length) return finish('No seat was selected. Take over on this page.', false);
      if (check === 'partial') return finish('Only one of the two seats got selected (' + list(now) + '). Fix it yourself.', false);
      return finish('BookMyShow selected different seats (' + list(now) + '). Clear or fix them yourself.', false);
    } finally { busy = false; }
  }

  let timer = 0;
  async function start() {
    state('loading', 'Waiting for the seat map…', false);
    const ready = await waitFor(() => !!stage() && allGroups().length > 0, 25000, 300);
    if (finished) return;
    if (!ready) {
      const stop = stopNow();
      return finish(stop ? stop.message : 'The seat map did not load in 25 seconds. Reload the page or take over.', false);
    }
    let ticks = 0;
    timer = setInterval(() => {
      if (finished || busy) return;
      ticks++;
      const stop = stopNow();
      if (stop) { latched = latched || stop.code !== 'terms'; ids = []; state('stopped', stop.message, false); if (latched) { clearRings(); } return; }
      if (latched) return;
      if (!pair) { state('nopair', 'This alert names no exact pair. Pick seats yourself.', false); return; }
      if (Date.now() - lastVerify >= 2000 || ticks === 1) verify();
      try { drawRings(); } catch (error) { /* the map is redrawing; try again next tick */ }
    }, 500);
    window.addEventListener('scroll', () => { try { drawRings(); } catch (error) { /* ignore */ } }, { passive: true });
    window.addEventListener('resize', () => { try { drawRings(); } catch (error) { /* ignore */ } });
  }

  function init(detail) {
    if (started) return;
    started = true;
    token = String(detail.token || '');
    pair = detail.pair || null;
    if (pair && !(Array.isArray(pair.seats) && pair.seats.length === 2 && pair.row)) pair = null;
    post('hello', {});
    if (!pair) { state('nopair', 'This alert names no exact pair. Pick seats yourself.', false); return; }
    start();
  }

  window.addEventListener('message', (event) => {
    const d = event.data;
    if (event.source !== window || !d || d.src !== 'screenwatch-content' || !started || d.token !== token || !token) return;
    if (d.type === 'select') select();
    else if (d.type === 'dismiss') { finished = true; clearInterval(timer); clearRings(); }
  });
  window.addEventListener('screenwatch-init', (event) => {
    let detail = null;
    try { detail = JSON.parse(event.detail); } catch (error) { return; }
    if (detail && typeof detail.token === 'string' && detail.token.length >= 16) init(detail);
  });

  if (TEST) {
    const raw = new URLSearchParams(location.search).get('task');
    if (raw) { try { const task = JSON.parse(raw); init({ token: 'test-token-test-token', pair: task.pair || null }); } catch (error) { /* bad test task */ } }
  }
})();
