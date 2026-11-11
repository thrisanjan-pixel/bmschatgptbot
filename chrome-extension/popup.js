// Pop-up: the on/off switch (default off), the dashboard address, connection status, armed alerts with Cancel, the last result.
const DEFAULT_URL = 'https://bmschatgptbot-production.up.railway.app';
const $ = (id) => document.getElementById(id);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true });

async function render() {
  const c = await chrome.storage.local.get({ enabled: false, dashboard: DEFAULT_URL, status: null, armed: [], last: null });
  $('enabled').checked = !!c.enabled;
  if (document.activeElement !== $('dashboard')) $('dashboard').value = c.dashboard;
  const status = $('status');
  status.textContent = !c.enabled ? 'Off. Nothing runs on this PC.' : c.status ? c.status.text : 'Connecting…';
  status.className = 'status ' + (c.enabled && c.status ? (c.status.ok ? 'ok' : 'bad') : '');
  const list = $('alerts');
  list.replaceChildren();
  if (!c.armed.length) { const li = document.createElement('li'); li.className = 'muted'; li.textContent = 'None. Arm "Open on PC" on an alert in the dashboard.'; list.append(li); }
  for (const alert of c.armed) {
    const li = document.createElement('li'), text = document.createElement('span'), button = document.createElement('button');
    text.textContent = (alert.movie || 'Alert') + (alert.until ? ' · until ' + clock(alert.until) : '');
    button.textContent = 'Cancel'; button.className = 'plain';
    button.onclick = async () => {
      button.disabled = true;
      const reply = await chrome.runtime.sendMessage({ type: 'cancel', alert: alert.id });
      $('err').textContent = reply && reply.ok ? '' : 'Could not cancel: ' + ((reply && reply.error) || 'unknown error');
      render();
    };
    li.append(text, button); list.append(li);
  }
  $('last').textContent = c.last ? c.last.text + ' (' + new Date(c.last.at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit' }) + ')' : 'Nothing yet.';
}

$('enabled').addEventListener('change', async () => {
  // Switching on starts from "now": earlier alerts are never replayed.
  await chrome.storage.local.set({ enabled: $('enabled').checked, since: null });
  await chrome.runtime.sendMessage({ type: 'sync' });
  render();
});

$('save').addEventListener('click', async () => {
  $('err').textContent = '';
  let url;
  try { url = new URL($('dashboard').value.trim()); } catch (error) { $('err').textContent = 'Enter a full https address.'; return; }
  if (url.protocol !== 'https:' || url.username || url.password) { $('err').textContent = 'The address must start with https:// and have no password in it.'; return; }
  const base = url.origin;
  if (base !== DEFAULT_URL) {
    // Must run inside this click: Chrome only shows its permission prompt for a user action.
    const granted = await chrome.permissions.request({ origins: [base + '/*'] });
    if (!granted) { $('err').textContent = 'Chrome permission for ' + base + ' was not granted.'; return; }
  }
  await chrome.storage.local.set({ dashboard: base, since: null, status: null });
  await chrome.runtime.sendMessage({ type: 'sync' });
  $('dashboard').value = base;
  render();
});

chrome.storage.onChanged.addListener(render);
render();
