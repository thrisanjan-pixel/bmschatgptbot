// Runs in the page's own (MAIN) world so it can reach the seat map's Konva stage. It does nothing until content.js sends a
// token and a pair for a tab the extension opened. It reports when the exact pair has been selected.
(function () {
  'use strict';
  if (window.__screenwatchPage) return;
  window.__screenwatchPage = true;
  const L = globalThis.ScreenwatchSeatLogic;
  // Test hook: only on a file: page that set the flag itself (test/fixture.html). Never active on a website.
  const TEST = window.__SCREENWATCH_TEST__ === true && location.protocol === 'file:';
  let token = '', pair = null, started = false, finished = false, busy = false, latched = false, autoCheckout = false;
  let allowedRows, allowedCategories, partySize, autoAcceptTerms = false, acceptedTerms = false;
  let rowCategories = new Map();
  let lastKey = '', lastVerify = 0, ids = [], overlay = null, rings = [];

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (node) => !!node && (node.offsetParent !== null || (node.getClientRects && node.getClientRects().length > 0));
  const groupsOn = (stage) => stage.find('Group').filter((g) => g.attrs && g.attrs.seatObj && /^Seat-/.test(String(g.attrs.id || '')));
  const stage = () => (window.Konva?.stages || []).find((candidate) => visible(candidate.content) && groupsOn(candidate).length) || null;

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

  const allGroups = () => { const s = stage(); return s ? groupsOn(s) : []; };
  const isSelected = (g) => /-selected$/.test(String(g.attrs.id));
  const seatData = (group) => {
    const seat = group.attrs.seatObj;
    const category = String(seat.priceDesc ?? '').trim() || rowCategories.get(String(seat.rowNumber ?? '').trim().toUpperCase());
    return { ...seat, priceDesc: category };
  };
  const seatObjs = () => allGroups().filter((g) => !isSelected(g)).map((g) => {
    const { x, y, width, height } = g.getClientRect();
    return { ...seatData(g), id: g.attrs.id, x: x + width / 2, y: y + height / 2, width };
  });
  const selectedGroups = () => allGroups().filter(isSelected);
  const groupById = (id) => allGroups().find((g) => g.attrs.id === id && !isSelected(g));

  function screenRect(group) {
    const s = stage(), r = group.getClientRect(), b = s.content.getBoundingClientRect();
    const scale = (s.width && s.width()) ? b.width / s.width() : 1;
    return { x: b.left + r.x * scale, y: b.top + r.y * scale, w: r.width * scale, h: r.height * scale };
  }
  function clickSeat(group) {
    const r = screenRect(group), x = r.x + r.w / 2, y = r.y + r.h / 2;
    group.findOne('Rect')?.fire('click', { evt: new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }) }, true);
  }

  // Page conditions for detectStop. Reads text and the DOM only.
  const termsLabel = (node) => node.textContent.replace(/\s+/g, ' ').trim();
  const acceptLabel = /^(accept|i accept|i agree|agree|accept (&|and) continue)$/i;
  const modalSelector = 'dialog[open],[role="dialog"],[aria-modal="true"]';

  function cinemaTermsDialog() {
    const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,strong,b,div,span,[role="heading"]')]
      .filter((node) => visible(node) && /^(?:(?:cinema|theatre) )?terms\s*(?:&|and)\s*conditions\s*:?$/i.test(termsLabel(node))
        && ![...node.children].some((child) => termsLabel(child) === termsLabel(node)));
    if (headings.length !== 1) return null;
    const heading = headings[0], modal = heading.closest(modalSelector);
    for (let panel = heading.parentElement, depth = 0; panel && panel !== document.body && depth < 10; panel = panel.parentElement, depth++) {
      const controls = [...panel.querySelectorAll('button,a,[role="button"],div,span')].filter((node) =>
        (acceptLabel.test(termsLabel(node)) || /^cancel$/i.test(termsLabel(node))) && visible(node)
        && (node.matches('button,a,[role="button"]') || getComputedStyle(node).cursor === 'pointer')
        && ![...node.children].some((child) => visible(child) && termsLabel(child) === termsLabel(node)
          && (child.matches('button,a,[role="button"]') || getComputedStyle(child).cursor === 'pointer')));
      const accepts = controls.filter((node) => acceptLabel.test(termsLabel(node)));
      if (accepts.length && (modal || controls.some((node) => /^cancel$/i.test(termsLabel(node))))) {
        return { accept: accepts.length === 1 ? accepts[0] : null };
      }
      if (panel === modal) break;
    }
    return { accept: null };
  }

  function termsWaiting() {
    if (cinemaTermsDialog()) return true;
    const buttons = Array.from(document.querySelectorAll('button,[role=button],a')).filter((b) => visible(b) && acceptLabel.test(termsLabel(b)));
    return buttons.some((b) => { let n = b; for (let i = 0; i < 6 && n; i++, n = n.parentElement) if (/terms/i.test((n.textContent || '').slice(0, 4000)) && n !== document.body) return true; return false; });
  }

  function clickCinemaTerms() {
    if (!autoAcceptTerms || acceptedTerms) return;
    const button = cinemaTermsDialog()?.accept;
    if (!button || button.closest(':disabled,[disabled],[aria-disabled="true"],[inert]')) return;
    const rect = button.getBoundingClientRect();
    if (!button.contains(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2))) return;
    acceptedTerms = true;
    button.click();
  }
  function stopNow() {
    return L.detectStop({
      url: TEST ? undefined : location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 30000),
      hasChallenge: !!document.querySelector('iframe[src*="challenges.cloudflare.com"],#challenge-form'), hasTerms: termsWaiting()
    });
  }

  function quantityPopup() {
    const label = (node) => (node.textContent || '').replace(/\s+/g, ' ').trim();
    const matching = (scope, pattern) => [...scope.querySelectorAll('button,a,li,div,span,p,b,strong,label,h1,h2,h3,h4,h5,h6,[role]')]
      .filter((node) => pattern.test(label(node)) && visible(node)
        && ![...node.children].some((child) => label(child) === label(node) && visible(child)));
    const titles = matching(document, /^how many seats\s*\?$/i);
    if (!titles.length) return null;
    if (titles.length !== 1) return {};
    const title = titles[0];
    const dialog = title.closest('dialog,[role="dialog"],[aria-modal="true"]');
    for (let panel = title.parentElement; panel && panel !== document.body; panel = panel.parentElement) {
      const buttons = matching(panel, /^select seats$/i);
      if (buttons.length === 1) {
        const choices = matching(panel, /^(?:[1-9]|10)$/);
        const requested = choices.filter((node) => label(node) === String(partySize));
        if (requested.length === 1 && new Set(choices.map(label)).size >= 2) {
          return { button: buttons[0], quantity: requested[0] };
        }
      }
      if (panel === dialog) break;
    }
    return {};
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
    const candidates = L.rankBlocks(seatObjs(), partySize, pair, allowedRows, allowedCategories);
    const best = candidates[0];
    ids = best?.ids || [];
    if (best) state('verified', 'Available seats: ' + best.pair.row + ' ' + best.pair.seats.join(' + ') + ' (' + best.pair.category + ').', true);
    else state('gone', 'No group of ' + partySize + ' adjacent seats matches your alert.', false);
    return candidates;
  }

  function finish(text, ok) { finished = true; clearInterval(timer); clearRings(); state(ok ? 'done' : 'error', text, false); post('result', { text, ok: !!ok }); }
  const list = (groups) => groups.map((g) => L.seatLabel(Object.assign({}, g.attrs.seatObj, { displaySeatNumber: g.attrs.seatObj.cinemaSeatNumber != null ? g.attrs.seatObj.cinemaSeatNumber : g.attrs.seatObj.displaySeatNumber }))).join(', ') || 'none';

  async function waitFor(test, ms, step) {
    const end = Date.now() + ms;
    while (Date.now() < end) { if (test()) return true; await sleep(step || 100); }
    return test();
  }

  async function openCheckout(text) {
    const seatPath = location.pathname;
    const controlVisible = (node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility === 'visible';
    const deadline = Date.now() + 5000;
    state('selecting', text + ' Waiting for the checkout button…', false);
    while (!finished && location.pathname === seatPath) {
      const dialog = [...document.querySelectorAll(modalSelector)].some(controlVisible);
      const stop = stopNow();
      if (stop?.code === 'terms' && autoAcceptTerms && Date.now() < deadline) {
        clickCinemaTerms();
        await sleep(150);
        continue;
      }
      if (dialog || stop) return finish(stop?.message || text + ' Review the prompt on BookMyShow.', false);
      const controls = [...document.querySelectorAll('button,[role="button"]')]
        .filter(controlVisible)
        .map((element) => ({element, label: element.getAttribute('aria-label') || element.innerText,
          enabled: !element.matches(':disabled,[aria-disabled="true"]') && !element.closest('[inert]')}));
      const button = L.checkoutCandidate(controls);
      if (button) {
        const bounds = button.getBoundingClientRect();
        const top = document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
        if (top && button.contains(top)) {
          state('done', text + ' Opening BookMyShow checkout…', false);
          button.click();
          await waitFor(() => {
            if (finished || location.pathname !== seatPath) return true;
            const stop = stopNow();
            if (stop?.code === 'terms' && autoAcceptTerms) {
              clickCinemaTerms();
              return false;
            }
            return !!stop;
          }, 8000, 150);
          if (!finished && stopNow()?.code === 'terms') return finish(autoAcceptTerms
            ? 'BookMyShow’s terms dialog stayed open. Click Accept to continue.' : 'Review the cinema terms on BookMyShow.', false);
          finished = true;
          return;
        }
      }
      if (Date.now() >= deadline) break;
      await sleep(150);
    }
    if (!finished && location.pathname === seatPath) finish(text + ' Continue on BookMyShow.', false);
  }

  async function select() {
    if (busy || finished || latched || (!TEST && !autoCheckout && !navigator.userActivation.isActive)) return;
    busy = true;
    try {
      const stop = stopNow();
      if (stop) { latched = stop.code !== 'terms'; state('stopped', stop.message, false); return; }
      if (selectedGroups().length) return finish('You already have seats selected. Clear them first.', false);
      state('selecting', 'Selecting…', false);
      const popup = quantityPopup();
      if (popup) {
        if (!popup.quantity || !popup.button) {
          state('quantity-manual', 'Choose ' + partySize + ' and press Select Seats if BookMyShow offers that count.', false);
          return;
        }
        popup.quantity.click();
        await sleep(150);
        if (finished) return;
        const interrupted = stopNow();
        const updated = quantityPopup();
        if (interrupted || !updated?.button || updated.button.closest('[disabled],[aria-disabled="true"],[inert]')) {
          return finish(interrupted ? interrupted.message : 'The seat-count dialog changed. Take over on this page.', false);
        }
        updated.button.click();
        if (!(await waitFor(() => !quantityPopup(), 5000))) return finish('The seat-count pop-up did not close. Take over on this page.', false);
      }
      const ready = await waitFor(() => !!stopNow() || allGroups().length > 0, 12000, 200);
      if (finished) return;
      const blocked = stopNow();
      if (blocked) return finish(blocked.message, false);
      if (!ready) return finish('The seat map did not load after the quantity dialog. Take over on this page.', false);
      if (selectedGroups().length) return finish('You already have seats selected. Clear them first.', false);
      let best = verify()[0];
      if (!best) return;
      let firstGroup = groupById(best.ids[0]);
      if (!firstGroup) return finish('The available pair changed. Take over on this page.', false);
      clickSeat(firstGroup);
      await waitFor(() => selectedGroups().length >= partySize, 2000);
      if (finished) return;
      if (!selectedGroups().length && !stopNow()) {
        const missed = best.ids[0];
        best = L.rankBlocks(seatObjs(), partySize, pair, allowedRows, allowedCategories).find(({ ids }) => !ids.includes(missed));
        firstGroup = best && groupById(best.ids[0]);
        if (!firstGroup) return finish('No alternative group of adjacent seats matches your alert.', false);
        clickSeat(firstGroup);
        await waitFor(() => selectedGroups().length >= partySize, 2000);
        if (finished) return;
      }
      // BookMyShow may fill a group from the clicked seat in the opposite direction.
      const filled = selectedGroups();
      if (partySize > 1 && filled.length === partySize && !stopNow()
          && filled.some((g) => g.attrs.id === firstGroup.attrs.id + '-selected')
          && L.checkSelection(filled.map(seatData), best.pair) !== 'exact') {
        for (let remaining = filled.length; remaining > 0 && !finished && !stopNow(); remaining--) {
          const current = selectedGroups();
          if (!current.length) break;
          clickSeat(current[0]);
          if (!(await waitFor(() => selectedGroups().length < current.length, 700))) break;
        }
        if (!finished && !stopNow() && !selectedGroups().length) {
          best = verify()[0];
          if (!best) return;
          const opposite = groupById(best.ids.at(-1));
          if (opposite) {
            clickSeat(opposite);
            await waitFor(() => selectedGroups().length >= partySize, 2000);
          }
        }
      }
      for (const id of best.ids) {
        const selected = selectedGroups().map((g) => g.attrs.seatObj);
        if (finished || stopNow() || !selected.length || L.checkSelection(selected, best.pair) !== 'partial') break;
        const next = groupById(id);
        if (!next) continue;
        clickSeat(next);
        if (!(await waitFor(() => selectedGroups().length > selected.length, 1000))) break;
      }
      if (finished) return;
      const now = selectedGroups();
      const selected = now.map(seatData);
      const { category, row, seats } = best.pair;
      if (L.checkSelection(selected, best.pair) === 'exact'
          && selected.every((seat) => String(seat.priceDesc || '').trim().toUpperCase() === category.trim().toUpperCase())) {
        const afterSelection = stopNow();
        if (afterSelection && afterSelection.code !== 'terms') return finish(afterSelection.message, false);
        clearInterval(timer);
        clearRings();
        const text = 'Selected ' + row + ' ' + seats.join(' + ') + ' (' + category + ').';
        if (TEST && new URLSearchParams(location.search).get('checkout') !== '1') {
          finished = true;
          post('checkout-ready', { text });
        } else {
          await openCheckout(text);
        }
        return;
      }
      if (!now.length) return finish('No seat was selected. Take over on this page.', false);
      if (now.length < partySize) return finish('Only ' + now.length + ' of ' + partySize + ' seats were selected (' + list(now) + '). Continue on BookMyShow.', false);
      return finish('BookMyShow selected different seats (' + list(now) + '). Clear or fix them yourself.', false);
    } finally { busy = false; }
  }

  let timer = 0;
  async function start() {
    state('loading', 'Waiting for the seat map…', false);
    const ready = await waitFor(() => !!quantityPopup() || allGroups().length > 0, 25000, 300);
    if (finished) return;
    if (!ready) {
      const stop = stopNow();
      return finish(stop ? stop.message : 'The seat map did not load in 25 seconds. Reload the page or take over.', false);
    }
    let ticks = 0, mapWaitSince = Date.now();
    timer = setInterval(() => {
      if (finished || busy) return;
      ticks++;
      const stop = stopNow();
      if (stop) { latched = latched || stop.code !== 'terms'; ids = []; state('stopped', stop.message, false); if (latched) { clearRings(); } return; }
      if (latched) return;
      const popup = quantityPopup();
      if (popup) {
        mapWaitSince = Date.now();
        clearRings();
        if (popup.quantity && popup.button) state('quantity', 'Ready to choose ' + partySize + ' seats on BookMyShow.', true);
        else state('quantity-manual', 'Choose ' + partySize + ' and press Select Seats if BookMyShow offers that count.', false);
        return;
      }
      if (!allGroups().length) {
        if (Date.now() - mapWaitSince >= 25000) {
          finish('Screenwatch could not read this seat map. Continue on BookMyShow.', false);
          return;
        }
        state('loading', 'Waiting for the seat map…', false);
        return;
      }
      mapWaitSince = Date.now();
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
    allowedRows = detail.allowedRows === null || Array.isArray(detail.allowedRows) ? detail.allowedRows : undefined;
    allowedCategories = Array.isArray(detail.allowedCategories) ? detail.allowedCategories : [];
    partySize = detail.partySize ?? 2;
    autoCheckout = detail.autoCheckout === true;
    autoAcceptTerms = autoCheckout && detail.autoAcceptTerms === true;
    if (pair && !(Array.isArray(pair.seats) && pair.seats.length === 2 && pair.row)) pair = null;
    post('hello', {});
    if (!L) { finish('Screenwatch seat logic did not load. Reload this tab and try again.', false); return; }
    if (Array.isArray(detail.layoutRows)) rowCategories = L.categoriesByRow(detail.layoutRows);
    if (!L.canAutoCheckout({ pair, partySize: detail.partySize, allowedRows })) {
      const message = detail.partySize === 0
        ? 'Set “Minimum seats open” in your Screenwatch alert to enable automatic seat selection.'
        : 'Automatic selection needs a saved alert with a count of 1–10 seats.';
      state('nopair', message, false);
      return;
    }
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
    const query = new URLSearchParams(location.search);
    const raw = query.has('extension') ? null : query.get('task');
    if (raw) { try { const task = JSON.parse(raw); init({ token: 'test-token-test-token', pair: task.pair || null }); } catch (error) { /* bad test task */ } }
  }
})();
