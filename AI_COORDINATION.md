# Screenwatch handoff between Claude and Codex

Use the latest Screenwatch branch or PR named by the owner as the source of truth. The PC handoff checkout may be older and may contain local experiments. A proposed change stays pending until a commit or PR and its test result are recorded.

- Before editing, check the current branch, working tree, and remote tip. If the other assistant has work in progress in the same files, use a separate branch or worktree and record the overlap here.
- Keep each change small and on its own branch. Preserve uncommitted work. Review the other branch's diff before combining changes, especially where both edits touch the alert feed, PC helper, Chrome extension, or dashboard controls.
- For a handoff, record the repo, branch or PR, commit SHA, files changed, verification, and work still pending. Use the same information to coordinate the next deployment. Have one assistant handle a given deployment and record the Railway project, service, and deployed commit after verification.
- Keep tokens, passwords, browser sessions, and payment details out of this note.

Current handoff: Claude is working on the one-click seat-selection flow in the newer Screenwatch workspace. Its branch, commit, and live test result are pending. Codex should review that revision before making any follow-up changes. Railway sign-in is separate from GitHub access. Confirm the production project and service in Railway before deploying.

## Handoff from Claude, 11 Nov 2026

- Repo `thrisanjan-pixel/bmschatgptbot`, branch `main`. The commit "Add the Chrome seat helper" holds the server support and the new `chrome-extension/` folder. It builds on the uncommitted `pc_seat_pair` change that was in the working tree.
- Files: `watch_preferences.py`, `dashboard.py`, `pc_opener.py`, `static/screenwatch.html`, `tests/test_features.py`, `chrome-extension/*`.
- Verification: 290 Python tests and 14 node tests pass. The page script passed against the fake seat map in `chrome-extension/test/fixture.html`. The Konva facts it relies on were checked by hand on a live BookMyShow seat page.
- Pending: deploy to Railway (project f7f005f3, service 18cc5ac2) is waiting for the owner's approval. The extension has not been loaded in real Chrome yet, so the banner, the handshake and a real alert are unverified.
