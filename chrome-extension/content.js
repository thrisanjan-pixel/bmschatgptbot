// Isolated-world script. It asks the background worker whether THIS tab was opened by the extension for a fresh alert.
// If not, it does nothing at all. If so, it shows the banner and relays the user's button press to page.js.
(function () {
  'use strict';
  if (window.__screenwatchContent) return;
  window.__screenwatchContent = true;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function getTask() {
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const reply = await chrome.runtime.sendMessage({ type: 'get-task' });
        if (reply && reply.task && reply.task.event) return reply.task;
      } catch (error) { return null; }
      await sleep(500);
    }
    return null;
  }

  function when(event) {
    const show = event.show || {};
    const d = String(show.date || '');
    const date = /^\d{8}$/.test(d) ? new Date(d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8) + 'T12:00:00+05:30').toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', year: 'numeric' }) : d;
    return [date, show.time].filter(Boolean).join(' ');
  }

  async function main() {
    const task = await getTask();
    if (!task) return;
    const event = task.event, pair = event.pair || null, show = event.show || {};
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    const token = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

    const host = document.createElement('div');
    host.style.cssText = 'all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483647';
    const root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = '<style>' +
      '.bar{font:13px/1.35 system-ui,Segoe UI,sans-serif;background:#1f1b3a;color:#fff;border-bottom:3px solid #ff2d95;padding:6px 12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}' +
      '.info{flex:1 1 320px;min-width:0}.title{font-weight:700}.sub{color:#d6d3f5}.status{margin-top:2px;font-weight:600}' +
      '.status.ok{color:#86efac}.status.warn{color:#fcd34d}.status.bad{color:#fca5a5}' +
      'button{font:600 13px system-ui,sans-serif;border:0;border-radius:8px;padding:9px 14px;cursor:pointer}' +
      '#go{background:#ff2d95;color:#fff}#go:disabled{background:#4b4770;color:#9a97c0;cursor:not-allowed}#no{background:#ffffff1f;color:#fff}' +
      '</style><div class="bar"><div class="info"><div class="title" id="t"></div><div class="sub" id="s"></div><div class="status" id="st">Waiting for the seat map…</div></div>' +
      '<button id="go" disabled>Select seats and open checkout</button><button id="no">Dismiss</button></div>';
    const $ = (id) => root.getElementById(id);
    $('t').textContent = 'Screenwatch · ' + [show.movie, show.venue].filter(Boolean).join(' · ') + (when(event) ? ' · ' + when(event) : '');
    $('s').textContent = pair ? 'Section ' + pair.category + ' · Row ' + pair.row + ' · Seats ' + pair.seats.join(' + ') : (event.summary || '');
    document.documentElement.appendChild(host);

    const post = (type) => window.postMessage({ src: 'screenwatch-content', token, type }, location.origin);
    const say = (text, tone) => { const n = $('st'); n.textContent = text; n.className = 'status ' + (tone || ''); };
    let hello = false, gone = false, checkoutUntil = 0, attemptedCheckout = false;
    let autoPending = task.autoCheckout === true;

    function openCheckout(text) {
      const confirmed = !gone && !attemptedCheckout && Date.now() < checkoutUntil;
      checkoutUntil = 0;
      if (!confirmed) return;
      attemptedCheckout = true;
      $('go').disabled = true;
      const L = globalThis.ScreenwatchSeatLogic;
      const exactPage = L.validSeatUrl(location.href) && new URL(event.url).pathname === location.pathname;
      const visible = (node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility === 'visible';
      const dialog = [...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible);
      const stop = L.detectStop({url: location.href, title: document.title,
        text: (document.body?.innerText || '').slice(0, 30000),
        hasChallenge: !!document.querySelector('iframe[src*="challenges.cloudflare.com"],#challenge-form')});
      const controls = [...document.querySelectorAll('button,[role="button"]')]
        .filter(visible)
        .map((element) => ({element, label: element.getAttribute('aria-label') || element.innerText,
          enabled: !element.matches(':disabled,[aria-disabled="true"]') && !element.closest('[inert]')}));
      const button = exactPage && !dialog && !stop && L.checkoutCandidate(controls);
      if (!button) {
        say(text + ' Continue on BookMyShow.', 'warn');
        return;
      }
      const bounds = button.getBoundingClientRect();
      const top = document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
      if (!top || !button.contains(top)) {
        say(text + ' The checkout button is covered or off-screen. Continue on BookMyShow.', 'warn');
        return;
      }
      say(text + ' Opening checkout. Complete payment on BookMyShow.', 'ok');
      button.click();
    }

    window.addEventListener('message', (e) => {
      const d = e.data;
      if (e.source !== window || !d || d.src !== 'screenwatch-page' || d.token !== token) return;
      if (d.type === 'hello') { hello = true; return; }
      if (d.type === 'state') {
        say(String(d.text || ''), d.phase === 'verified' || d.phase === 'done' ? 'ok' : d.phase === 'loading' || d.phase === 'selecting' ? 'warn' : 'bad');
        $('go').disabled = attemptedCheckout || !d.canSelect || Date.now() < checkoutUntil;
        if (autoPending && d.phase === 'verified' && d.canSelect) {
          autoPending = false;
          selectPair();
        } else if (d.phase === 'gone' || d.phase === 'stopped') {
          autoPending = false;
          checkoutUntil = 0;
        }
        if (d.phase === 'stopped') chrome.runtime.sendMessage({ type: 'result', ok: false, text: String(d.text || '') }).catch(() => {});
      } else if (d.type === 'result') {
        checkoutUntil = 0;
        $('go').disabled = true;
        chrome.runtime.sendMessage({ type: 'result', ok: !!d.ok, text: String(d.text || '') }).catch(() => {});
      } else if (d.type === 'checkout-ready') {
        openCheckout(String(d.text || ''));
      }
    });

    // Only a real click by the person counts. Script-made clicks have isTrusted false and are ignored.
    function selectPair() {
      $('go').disabled = true;
      checkoutUntil = Date.now() + 15000;
      post('select');
    }
    $('go').addEventListener('click', (e) => {
      if (!e.isTrusted || $('go').disabled || attemptedCheckout) return;
      selectPair();
    });
    $('no').addEventListener('click', (e) => {
      if (!e.isTrusted) return;
      gone = true; post('dismiss'); host.remove();
      chrome.runtime.sendMessage({ type: 'dismiss' }).catch(() => {});
    });

    // Give page.js its token and the pair. It may load a moment after this script, so repeat until it answers.
    const detail = JSON.stringify({ token, pair, autoCheckout: autoPending });
    for (let i = 0; i < 12 && !hello && !gone; i++) {
      window.dispatchEvent(new CustomEvent('screenwatch-init', { detail }));
      await sleep(i < 4 ? 250 : 1000);
    }
    if (!hello && !gone) say('Could not start on this page. Reload it, or take over.', 'bad');
  }
  main();
})();
