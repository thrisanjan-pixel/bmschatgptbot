# Screenwatch seat helper (Chrome extension)

When an alert with "Open on PC" armed fires, this extension opens the BookMyShow seat page in a new tab, highlights the two seats the alert named and waits. **You** press "Select these two seats". You press Pay yourself.

## Install

1. Open `chrome://extensions` in Chrome.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `chrome-extension` folder.
4. Open the extension's pop-up (puzzle icon, then "Screenwatch seat helper") and switch **Enabled on this PC** on. It is off by default and nothing runs while it is off.
5. Sign in to your Screenwatch dashboard in this same Chrome. The extension uses the dashboard's own session cookie.

The dashboard address is in the pop-up. Changing it to another https address makes Chrome ask for permission to talk to that address.

## Use

1. In the dashboard, switch **Open on PC** on for an alert. It stays armed until the alert opens on this PC or you turn it off; there is no time limit. Arming only works while the extension is running on this PC.
2. For an exact pair, the alert must be a row alert with **Seats together = 2**. The alert message then names the pair ("Best 2 together"), and the banner shows the section, row and seat numbers. Without a pair the banner says "This alert names no exact pair. Pick seats yourself." and has no button.
3. When the alert fires, a tab opens and Chrome comes to the front with a notification and a spoken "Seats open. Check Chrome."
4. Check the banner at the top and the two pink rings on the seats. The button enables only while both seats are verified free. Press **Select these two seats**. The banner then says whether it worked. Review the seats and press Pay yourself.
5. The dashboard switch turns itself off after the page opens.

## Cancel

Press **Cancel** next to the alert in the extension pop-up, or turn the switch off on the dashboard card.

## What it never does

- It acts on BookMyShow only in a tab it opened for a fresh armed alert (under 10 minutes old), and only for the show that alert names.
- It selects seats only after you press its button (it checks the click is a real one).
- It never clicks Pay, Proceed, or any payment, terms or consent control. It never fills a form.
- It never reads or sends cookies or page data to the dashboard. The dashboard only receives "opened" and "cancel" for an alert.
- It never retries around a sign-in page, CAPTCHA, queue or waiting room, Cloudflare check, "Access denied" page or terms pop-up. It stops and tells you where to take over. After you accept a terms pop-up, the highlighting resumes.
- If the pair is no longer free it stops. It never picks other seats. If BookMyShow picks different neighbours, it tells you and leaves them for you to fix.

## Troubleshooting

- **Pop-up says "Sign in to the dashboard in this Chrome first":** open your dashboard in this Chrome and sign in.
- **Arming fails with "Enable the Screenwatch extension in Chrome on this PC first":** the extension is off, or has not polled in the last 2 minutes. Check the pop-up status.
- **No tab opened:** the alert's message was older than 10 minutes, had no seat-layout link, or the switch had expired.
- **Banner says "Could not start on this page":** reload the tab.
- **"Row label is not unique":** the row letter appears in more than one section. Choose seats yourself.
- Chrome may pause the service worker; it wakes every 30 seconds and polls every 3 seconds while an alert is armed.

## Tests

- Logic: `node --test chrome-extension/test/seatlogic.test.js` (from the project folder).
- Syntax: `node --check chrome-extension/<file>.js`.
- Seat-map driver: `test/fixture.html` is a fake Konva seat map with a seat-count pop-up and a fake Pay button that records any click. Open it as a file with a `task` parameter, for example
  `fixture.html?task={"pair":{"category":"Gold","row":"B","seats":["5","6"]}}`
  Options: `noqty=1`, `taken=6`, `dup=1`, `pick=left`, `terms=1`, `signin=1`, `late=1`, `autopress=2` (presses the simulated banner button after 2 seconds). The log shows what `page.js` reports and "Pay clicked" must stay "no". The `task` hook works only on a `file:` page that sets `window.__SCREENWATCH_TEST__` itself, so it is never active on a website. Headless example: `chrome --headless=new --allow-file-access-from-files --virtual-time-budget=12000 --dump-dom "file:///.../fixture.html?task=...&autopress=3"`.
