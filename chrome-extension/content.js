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
      '<button id="go" disabled>Select these two seats</button><button id="no">Dismiss</button></div>';
    const $ = (id) => root.getElementById(id);
    $('t').textContent = 'Screenwatch · ' + [show.movie, show.venue].filter(Boolean).join(' · ') + (when(event) ? ' · ' + when(event) : '');
    $('s').textContent = pair ? 'Section ' + pair.category + ' · Row ' + pair.row + ' · Seats ' + pair.seats.join(' + ') : (event.summary || '');
    document.documentElement.appendChild(host);

    const post = (type) => window.postMessage({ src: 'screenwatch-content', token, type }, location.origin);
    const say = (text, tone) => { const n = $('st'); n.textContent = text; n.className = 'status ' + (tone || ''); };
    let hello = false, gone = false;

    window.addEventListener('message', (e) => {
      const d = e.data;
      if (e.source !== window || !d || d.src !== 'screenwatch-page' || d.token !== token) return;
      if (d.type === 'hello') { hello = true; return; }
      if (d.type === 'state') {
        say(String(d.text || ''), d.phase === 'verified' || d.phase === 'done' ? 'ok' : d.phase === 'loading' || d.phase === 'selecting' ? 'warn' : 'bad');
        $('go').disabled = !d.canSelect;
        if (d.phase === 'stopped') chrome.runtime.sendMessage({ type: 'result', ok: false, text: String(d.text || '') }).catch(() => {});
      } else if (d.type === 'result') {
        $('go').disabled = true;
        chrome.runtime.sendMessage({ type: 'result', ok: !!d.ok, text: String(d.text || '') }).catch(() => {});
      }
    });

    // Only a real click by the person counts. Script-made clicks have isTrusted false and are ignored.
    $('go').addEventListener('click', (e) => { if (!e.isTrusted || $('go').disabled) return; $('go').disabled = true; post('select'); });
    $('no').addEventListener('click', (e) => {
      if (!e.isTrusted) return;
      gone = true; post('dismiss'); host.remove();
      chrome.runtime.sendMessage({ type: 'dismiss' }).catch(() => {});
    });

    // Give page.js its token and the pair. It may load a moment after this script, so repeat until it answers.
    const detail = JSON.stringify({ token, pair });
    for (let i = 0; i < 12 && !hello && !gone; i++) {
      window.dispatchEvent(new CustomEvent('screenwatch-init', { detail }));
      await sleep(i < 4 ? 250 : 1000);
    }
    if (!hello && !gone) say('Could not start on this page. Reload it, or take over.', 'bad');
  }
  main();
})();
