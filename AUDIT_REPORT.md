# BMS bot audit — 18 November 2026

**Latest status:** native-client deployment `afc94928-4825-4127-be41-9cd04db5e56f` is live and SUCCESS. Its 15-minute observation passed: 30 HTTP 200s, one 403 warning, five wrong-date responses discarded, zero crashes/error-level records, and unchanged urgent alert settings. The final section contains the accepted result; earlier sections preserve the investigation and rollback history.

At the first audit, changes were local and uncommitted on `codex/seat-helper-checkout` (base `4a1b430`). Existing uncommitted work was retained. No deployment, restart, commit, push, alert edit or Telegram test message had been performed at that stage.

## Bugs corrected

Line numbers in the original table refer to the working files at the first audit. The follow-up below records subsequent changes; neither revision has been deployed.

| Location | Cause | Fix and regression evidence |
| --- | --- | --- |
| `http_source.py:12`, `http_source.py:65` | Listing lanes could use separate budgets and gaps shorter than the requested policy. | Shared 30.0-second minimum and 120-attempt rolling-hour ceiling across listing, manual and metadata lanes. The aggregate boundary and restart budget tests pass. Manual seat reads also have a 30-second minimum; automatic seat reads retain their 60-second gap. |
| `api_watch.py:165`, `api_watch.py:370` | Watched jobs used 60 seconds and scheduled their next check from response completion, adding response latency each cycle. | Watched jobs use 30 seconds and schedule from attempt start. A simulated three-second response leaves the next due time at start + 30, not start + 33. |
| `api_watch.py:335` | Source-budget/cooldown waits woke once per second, adding scheduler rounding and unnecessary polling. | Wait until the persisted deadline, interruptible by shutdown. The full-deadline regression test passes. |
| `api_watch.py:394`, `api_watch.py:409`, `telegram.py:164` | An unconditional post-check pause and one-message sender turns delayed delivery. | Signal the sender after the transaction commits, remove the extra source-loop sleep, and flush due messages consecutively. Clear the wake event before processing to avoid losing a notification while the sender is working. Queue completion tests pass. |
| `row_watch.py:136` | Matching seat-count/map messages could be held for up to 480 seconds waiting for other maps. | New matches queue immediately, even when another map is pending. Regression test verifies `next_try == 0`. Legacy hold helpers remain compatible with existing stored messages. |
| `row_watch.py:312`, `row_watch.py:375` | Row matches relied on another process's next delivery tick. Sender exceptions could also be mistaken for source-read failures. | Flush directly after the successful seat transaction, outside the source exception handler. The sender-failure test preserves successful seat data without a sleep. |
| `row_watch.py:330` | A queued job could read a showtime that had since been marked inactive. | Require an active, unexpired session before reading. Removed-showtime regression test verifies no source request. |
| `http_source.py:81` | A 403 carrying Retry-After was not persisted at host level. | Persist the host deadline for 403 with Retry-After as well as 429. Existing per-job 403 backoff, repeated-refusal cooldown, rolling budget and recovery remain enabled. Deadline regression tests pass. |
| `telegram.py:141` | Retry deadlines used the timestamp before a potentially slow network send. | Start Retry-After when the failure response arrives. The slow-send test verifies the full remaining wait. |
| `telegram.py:93`, `telegram.py:155` | A flood wait on an optional photo after successful long text was swallowed, allowing the next queued message to send. | Mark the primary text delivered and persist the photo's global flood wait. The regression test verifies no duplicate text and no next message during the wait. |
| `api_source.py:73`, `api_source.py:121` | Malformed JSON shapes could cause incidental attribute/type exceptions. | Reject non-object JSON and malformed showtime identity with controlled ReadError failures. Malformed-shape tests pass. |
| `watch_preferences.py:112`, `watch_preferences.py:135` | A successful check on another date could make a selected-date alert appear healthy and distort its gap report. | Health and interval reports use the requested dates, falling back to undated discovery only where a dated job is absent. Regression test prevents an unrelated successful date hiding a failed selected date. |
| `http_source.py:43`, `row_watch.py:14` | Simultaneous startup migrations could both try to add `manual` and fail with duplicate-column errors. | Tolerate that specific concurrent migration result; propagate other SQLite errors. Both migration race regressions pass. The existing outbox migration guard in `store.py` was retained. |
| `health_watch.py:78`, `health_watch.py:93` | Worker termination leaked the SQLite connection; outer failures were silently retried. | Close in finally and log the failure type before retrying. Worker shutdown/resource tests pass. |
| `dashboard.py:44`, `static/screenwatch.html:1816` | Queue estimates and policy descriptions still assumed one-minute listing checks. | Calculate from the shared 30-second constant and describe the 120/hour cap. |
| `static/screenwatch.html:1843`, `static/screenwatch.html:1853` | Queue fetch failures were silent; the timer could keep labelling stale data live. | Add a bounded request, check HTTP status and retain a visible connection-lost state across timer renders. Dashboard JavaScript regression passes. |
| `chrome-extension/background.js:20`, `chrome-extension/background.js:33` | Dashboard reads/writes without a deadline could stall the extension polling loop. | Bound requests to ten seconds, retaining the existing shorter optional-map deadline. Timeout-signal tests pass. |
| `chrome-extension/background.js:113` | Opened tabs were remembered only at the end of a batch; a later failure could replay an already opened alert. | Persist each opened event and pending acknowledgement immediately. Partial-batch failure regression passes. |
| `chrome-extension/background.js:68`, `chrome-extension/background.js:154`, `chrome-extension/popup.js:5` | Rejected notification, storage, alarm or message promises could be unhandled or leave the popup without an error. | Catch background/message failures, reply with errors, display popup failures, and make optional notification rejection independent of tab success. Extension failure-path tests pass. |
| `city_watch.py:218` | Top-level Linux-only fcntl import prevented Windows test collection. | Import inside the Linux browser-worker entry point. Collection succeeds; POSIX process-group tests are explicitly skipped on Windows. |

The existing event/date identity guard at `api_source.py:126` was retained. An added worker-level regression proves a wrong-date payload creates neither a snapshot nor an outbox message and does not mark the job successfully checked. Previous matching/filtering and partial-availability fixes were preserved.

## Validation

- Full `python -m pytest -q --basetemp=runtime/test-temp/pytest-complete`: **369 passed, 5 skipped, 8 subtests passed**, 33.68 seconds. Warnings are errors through `pytest.ini`; zero test failures or warnings. The five skips are the POSIX process-group test inherited by row test subclasses on Windows.
- Node seatlogic, background, popup and dashboard checks: **25 passed, 0 failed**, including the required `chrome-extension/test/seatlogic.test.js` (20 tests).
- All Chrome extension JavaScript and inline dashboard scripts parse successfully. `git diff --check` passes.
- Tests use workspace-local packages via `PYTHONPATH=runtime/audit-deps;runtime/checkout-preview-deps;.`; pytest 9.2.0 was installed under ignored `runtime/`, not system-wide. `tests/conftest.py` keeps temporary test databases in the writable workspace.
- Corrected two existing test assumptions: the once-alert fixture was recreating a migrated legacy alert; a Telegram retry test was bypassing the global flood wait. Updated old 60-second and 10-second budget assertions to the requested 30-second policy.

## Read-only production observations

- Railway project `252b710e-08ef-4b22-a50c-5cbc32e08edd`, service `40663302-289f-470d-9c01-567dfe4a4556`, production environment `d7d9b0f6-1e8e-4c56-984f-4441a79810c6`.
- 239 log entries from **05:51:46–07:50:54 UTC, 18 November** (11:21:46–13:20:54 IST): **35 HTTP 403 entries, 35 event/date mismatch entries, no HTTP 429 entries and no Python traceback entries** in this sample. This is a bounded log sample, not an error-rate denominator or a guarantee about every worker.
- Separate latest request-history sample: 12 listing attempts, all HTTP 200. Its 11 request-start gaps were **30.027260–30.040789 seconds**, mean **30.032014 seconds**. No gap was below 30 seconds. These measurements are from the existing live build, not this undeployed patch.
- Deterministic local tests verify exactly 30-second reservation boundaries, a 120-attempt rolling-hour ceiling across lanes, and no added response-time drift. Real scheduling and network latency prevent a promise of exactly 30.000 seconds on every wall-clock interval. Backoff, competing alerts and metadata reads increase per-alert intervals; there is no faster catch-up burst.
- Urgent Avengers: Doomsday alert remains active for **20261217**, cinema **ALUC**, screen **DOLBY CINEMA**, English, existing 2D/3D/DOLBY CINEMA 3D formats, once enabled and urgent enabled. Dune alert remains active. Pending delivery count at inspection: zero. Neither alert was modified.

## Remaining limits and release status

- **Production is not at zero source errors.** BMS continues to issue intermittent 403s and sometimes supplies November 18 data when December 17 is requested. The bot rejects those mismatches. Safe backoff and recovery cannot force the external service to allow requests or supply bookings.
- No actual booking-open Dolby response was available to validate an end-to-end production alert. No real Telegram message or real Chrome booking flow was triggered. The tests use controlled responses and browser API doubles. Linux-only process termination still needs verification on Linux.
- Telegram delivery remains at least once: a crash after Telegram accepts a message but before SQLite acknowledges it can cause a duplicate retry. Claiming prevents normal concurrent sends; it cannot remove that remote acknowledgement ambiguity.
- This patch has **not** been deployed, restarted, committed or pushed. Deployment and live restart require the owner's explicit approval. Fresh post-deployment spacing and recovery measurements remain pending that approval.

## Follow-up: BMS session investigation and local soak

The live service and its alert settings remain untouched. This work continues in the same working tree and retains the earlier uncommitted changes.

### What the comparison establishes

1. A fresh read-only Railway log sample contains 240 entries from **06:03:46–08:03:25 UTC on 18 November**: 173 successful discovery checks, 31 HTTP 403 responses and 36 event/date mismatches. The **same event/date combinations both succeed and fail**. For example, `ET00439706 / 20261217` has 49 usable checks and 4 refusals; `ET00526306 / 20261217` has 37 usable checks and 8 refusals. This is not a consistently missing movie or an invalid endpoint for every request.
2. Live refusals have `Server: cloudflare` and HTML content type; successful local reads have JSON content type. The existing client opens a new urllib connection per read and retains no response cookies, so its cookie session age is effectively zero. Its bot User-Agent and HYD request region are constant. The rejection is at the HTTP response boundary, before JSON parsing. **A Cloudflare/front-door rejection is the leading explanation, not proof of a specific Cloudflare rule.** The available headers do not disclose whether IP reputation, traffic history, request context or another rule caused a particular refusal.
3. The local PC reaches Cloudflare's **HYD** edge; the captured Railway request history reaches **SIN**. That live history includes both 200 and 403 responses from SIN. The region/IP path is a confounder: local success cannot prove that the Singapore deployment's 403s are eliminated. No Railway region was changed.
4. A local baseline request with the unchanged bot headers returned 200 and correct December 17 identity. A pooled session with the same headers also returned 200 and received `__cf_bm` and `_cfuvid` cookies. A normal public explore-page visit returned 200 and additionally supplied `bmsId` and `platform`; a subsequent cookie-session API read with normal language/referrer context returned 200. Cookie **names and counts**, never cookie values, were recorded in the diagnostic comparison. These observations show that legitimate session reuse works; the small comparison does not establish that missing cookies were the sole cause of live 403s.
5. The site's previously captured official JavaScript names both v4 and v5 showtime routes. The observed v5 `primary-dynamic` route returned **400** with the bot's current event/date parameters, not equivalent usable showtime data. The verified v4 endpoint remains selected. No guessed endpoint, browser fingerprint spoofing, proxy rotation, challenge bypass, CAPTCHA solving or account change was used.
6. The six comparison requests were spaced **30.000240–30.000534 seconds** apart, calculated from their recorded start timestamps. One diagnostic script's per-row `gap` field was incorrectly written as zero after assigning the current timestamp; the comparisons here use differences between the independently recorded `at` values, not that field. The status sequence was **200, 200 (public page), 200, 400 (v5 probe), 200 (wrong date), 200**. The v5 probe is excluded from the subsequent production-path soak.

The log presentation had a separate confirmed root cause: the old `logging.basicConfig` sent INFO and WARNING output to stderr. Railway classifies stderr as error-level. Structured stdout logs now preserve the intended severity; genuine errors are not globally downgraded. This follows [Railway's documented log normalization](https://docs.railway.com/observability/logs).

### Follow-up changes

| Location | Change | Verification |
| --- | --- | --- |
| `bms_session.py:17`, `bms_session.py:42` | One thread-local Requests session reuses ordinary HTTPS connections and server-issued cookies. TLS verification stays enabled. HTTP adapter automatic retries and redirects are disabled. The existing honest bot User-Agent is retained. | Session/cookie reuse, fresh reset, timeout/error cleanup, approved host/scheme, no redirect/retry and TLS-default regression tests; real-source soak below. Connection and cookie reuse follow the [Requests session documentation](https://requests.readthedocs.io/en/latest/user/advanced/#session-objects). |
| `api_source.py:69`, `http_source.py:94` | Showtime API calls use the session transport, normal Accept-Language/referrer context and the existing persistent budget before every network attempt. No additional bootstrap page request is inserted before alerts. Catalog and seat transports keep their existing behavior. | A budget refusal test verifies that no pooled network call happens. Real reads validate event/date identity as before. |
| `api_watch.py:276`, `api_watch.py:297` | First 403 increments a persistent count, resets the current session and schedules exactly one retry at the next permitted 30-second slot. A second 403 removes the retry marker and backs off, normally 300 seconds for repeated refusal. Retry-After/429 and broad repeated-refusal cooldowns still take precedence. A later verified success resumes the normal 30-second due interval. | Tests cover one retry only, second-refusal backoff, persisted priority across connections, recovery, response-latency drift, Retry-After, 429 and no outbox/snapshot after refusal. |
| `api_watch.py:389`, `api_watch.py:414` | Success clears the retry marker. Wrong event/date responses are logged at INFO and discarded. 403s are warnings labelled `BMS blocked one check` with the cumulative count. | Captured log records verify severity and counters; wrong-date responses produce neither snapshots nor messages. |
| `log_config.py:7`, `api_watch.py:475`, `health_watch.py:75`, `row_watch.py:300`, `city_watch.py:231` | Single-line JSON logs go to stdout with explicit `info`/`warn` severity. All worker entry points use the shared setup. Genuine ERROR records retain ERROR severity. | Subprocess test verifies stdout output, empty stderr and correct severities; formatter test preserves genuine errors. No production logger was changed remotely. |
| `bms_session.py:49`, `http_source.py:77`, `dashboard.py:89` | Retain safe comparison metadata for the latest 200/403/429: endpoint, allowed request headers, session age, cookie count and allowed response headers, including CF-Ray. Store no cookie values, authentication or response bodies. The dashboard API exposes blocked counts and comparison metadata. | Privacy regression verifies cookie values and Set-Cookie are absent from stored observations. Dashboard counter test passes. |
| `watch_preferences.py:134`, `static/screenwatch.html:1844`, `static/screenwatch.html:2022`, `static/screenwatch.html:2071` | Health cards and queue/history text show `BMS blocked one check`; blocked/discarded checks do not get the error-style timeline marker. Wrong-date cards say `Waiting for requested date`. Blocked counts remain visible and unsuccessful checks are still counted honestly. | Python health/status tests and JavaScript rendering tests verify labels, retained counts and genuine unavailable-data states. |
| `requirements-cloud.txt`, `requirements-browser.txt`, `Dockerfile:10` | Pin Requests 2.34.2 and include the transport/logging modules in the existing image definition. | Local imports, full regression suites and Docker COPY path inspection. No image was deployed or live process restarted. |

### Local soak results

The real-endpoint soak **completed successfully in 900.594 seconds**. Its 31 request starts span **900.563232 seconds**, from **08:14:22.384–08:29:22.948 UTC on 18 November** (**13:44:22.384–13:59:22.948 IST**).

| Measurement | Result |
| --- | --- |
| HTTP 200 responses | **31** |
| HTTP 403 responses | **0** |
| HTTP 429 responses | **0** |
| Other HTTP statuses or transport failures | **0** |
| Wrong-date responses among the HTTP 200s | **3**, all discarded before ingest/snapshot/alert state |
| Usable event/date responses | **28** |
| Request-start gaps, minimum / mean / maximum | **30.011258 / 30.018774 / 30.024821 seconds** |
| Gaps below 30 seconds | **0** |
| Application error/critical-level log lines | **0** |
| Application warning-level log lines | **0** |
| Outbox messages created / Telegram messages sent | **0 / 0** |
| Soak stderr output | **0 bytes** |

The run exercised the actual Monitor/check/ingest path and normally scheduled discovered listing variants, using the dedicated `runtime/bms-soak-20261118/soak.sqlite3`, shared 120/hour and 30-second budgets, and one persistent session. Telegram credentials were cleared only in that child process. The production database was never opened. Raw request and structured application logs are saved beside the local database; all application records are INFO, including the three discarded-date records.

The full result and all 30 individual gap measurements are in `runtime/bms-soak-20261118/result.json`; request records are in `requests.jsonl`, application logs in `application.jsonl`, and the reproducing harness in `runtime/bms_soak.py`. No further 403 investigation/retry run was triggered because the complete soak recorded zero 403s.

**Limit:** this establishes a clean local 15-minute sample, not a guarantee of future behavior or of Railway/SIN behavior. Even the unchanged local baseline had returned 200. The exact rule behind intermittent production refusals remains opaque; the changes improve legitimate session handling and ensure a refusal is a counted warning with a bounded retry/backoff, rather than a crash, an availability alert or a falsely classified error. A production-region soak remains unrun because deployment and restart are expressly withheld.

A final read-only GET confirmed that the live ALLU alert remains **active, enabled, urgent, once**, with date **20261217**, screen **DOLBY CINEMA**, and the existing **2D / 3D / DOLBY CINEMA 3D** formats. No live settings were written.

### Follow-up validation

- `python -m pytest -q -rs --basetemp=runtime/test-temp/bms-session-release`: **384 passed, 5 skipped, 8 subtests passed in 35.35 seconds**, zero failures or test warnings. The skips are the POSIX process-group case inherited by five classes on Windows, as printed by pytest. Warnings remain configured as errors.
- `node --test chrome-extension/test/seatlogic.test.js chrome-extension/test/background.test.js chrome-extension/test/popup.test.js tests/dashboard.test.cjs`: **27 passed, zero failures, skips or warnings**. This includes the required 20 seatlogic tests.
- All inline dashboard JavaScript and extension JavaScript parse successfully. `git diff --check` passes.
- Full-suite output is retained in `runtime/bms-session-pytest.txt` and `runtime/bms-session-javascript.txt`. The recorded source comparison is in `runtime/bms-session-comparison.jsonl`; its valid start timestamps are used for timing calculations as explained above.
- No deploy, restart, commit, push, production alert edit or Telegram test message has occurred. Approval for any later deployment remains a separate decision.

## Approved deployment and immediate rollback — 18 November

The owner subsequently approved **“Deploy after soak”**. The gate passed: 384 Python tests and 27 JavaScript tests passed without failures or test warnings; the 900.594-second local soak had 31 HTTP 200s, zero 403s, zero error-level application records and gaps of 30.011–30.025 seconds.

- Uploaded the working tree with `railway up`, without a Git commit or push, to ItsKG project `252b710e-08ef-4b22-a50c-5cbc32e08edd`, service `40663302-289f-470d-9c01-567dfe4a4556`, production environment `d7d9b0f6-1e8e-4c56-984f-4441a79810c6`.
- Candidate deployment **`c47c144d-920b-4043-92bd-3b5ae5bb633a`** built and started all four workers. It was created at 08:31:03 UTC, with worker startup at approximately 08:31:41 UTC.
- **Live validation failed:** all eight source attempts on the candidate returned HTTP 403, including repeat attempts for previously usable December 17 listings. There were **zero HTTP 200s, zero wrong-date responses, zero crashes and zero error-level records** on that candidate. All eight refusals were counted WARN records, as intended. Actual request gaps were **30.028414–30.034543 seconds**; the new transport was not exceeding the pacing limit.
- Because source access had become completely unavailable despite healthy worker heartbeats, treated this as an alert-availability regression and invoked the authorized rollback immediately rather than leave the candidate running for the full ten minutes.
- Rolled back to the exact prior deployment **`be49a1ba-e8de-42f2-be04-52d0b6cff7da`**. Railway created successful rollback deployment **`479e26b7-6c4c-4e8a-a227-462928c4a13c`** at 08:35:37 UTC. Its image digest is **`sha256:f7e706f0fd28da51038e23d88889ef23ea5e502a621b256971bbd27b6c7eca32`**, matching the previous image exactly. The candidate is REMOVED.
- The restored image immediately resumed HTTP 200 reads on the same SIN edge. This strengthens the evidence that the candidate's transport/header/session changes are unsuitable on the Railway path; it does not isolate which changed request property triggered the source's policy. The locally clean session transport must not be represented as a production fix.
- The urgent December 17 ALLU Dolby alert was compared against a pre-deployment snapshot using full preference equality. It remained **active with identical settings** before deployment, on the candidate, and after rollback. Telegram remained configured and no test messages were sent.
- Continued read-only observation of the restored build through the remainder of the ten-minute rollout window. Candidate logs are saved in `runtime/failed-deploy-logs.jsonl`; image verification in `runtime/deploy-final-images.json`.

**Current live result: previous build restored; this patch is not live.** The candidate has not passed production validation. Local uncommitted changes remain preserved and no Git commit or push was made. The original image also restores its previous logging behavior; its stderr INFO/WARNING messages may again appear as error-level records in Railway. That presentation issue must not be confused with a traceback or crash.

### Completed ten-minute rollout/rollback observation

Observed **600.759 seconds**, 08:31:52–08:41:52.759 UTC (14:01:52–14:11:52.759 IST). The failed candidate was deliberately not left live for ten minutes: the immediate-rollback requirement took precedence, and monitoring continued on the restored image.

| Measurement | Aborted candidate | Restored previous image |
| --- | --- | --- |
| HTTP 200 | **0** | **11** |
| HTTP 403 | **8** | **1** |
| Wrong-date payloads within HTTP 200 | **0** | **1**, discarded |
| Request gap min / mean / max | **30.028414 / 30.031492 / 30.034543 s** | **30.030988 / 30.240315 / 32.293132 s** |
| Tracebacks or worker crashes | **0** | **0** |
| Genuine application ERROR records | **0** | **0** |
| Railway records classified error-level | **0** | **13**, consisting of 11 INFO and 2 WARNING messages routed through the old stderr logger |

No observed source request gap was below 30 seconds, including the rollback transition. The restored build's 11 HTTP 200s include one wrong-date response, so ten were usable showtime responses. The separate catalog check is not included in the showtime request count.

At the final check: listing state **running**, heartbeat age **2.37 seconds**, urgent alert **active with complete preference equality**, Telegram configured, **zero pending messages**, and no delivery failures. The restored image still has intermittent external BMS blocking and the original log-presentation defect; this rollback is recovery from the regression, not a claim that the original issue is solved.

Saved measurements: `runtime/live-deploy-watch/rollback-result.json` and `all-rollout-requests.json`; restored logs: `runtime/restored-deploy-logs.jsonl`. The initial local watcher had a missing optional dashboard-password lookup; it was corrected before observation began, used the existing passwordless service configuration, and did not crash or change the live bot. The manually aborted candidate watcher was stopped when rollback was invoked; the replacement read-only watcher completed normally.

The new evidence rules out a pacing burst as the cause of this failed rollout and shows that a clean local/HYD soak is insufficient validation for a changed request transport on Railway/SIN. The next transport revision must preserve the known-working native client behavior and be validated on that path before another production rollout. **No second candidate deployment was attempted.**

## Native-client restoration round — approved “Yes, redeploy”

The owner then authorized another working-tree deployment, with rollback if the first four source attempts have no HTTP 200 or any crash occurs, and a 15-minute rate comparison if it holds.

### Request-layer comparison

Compared the archived uploaded source for `be49a1ba` (the exact image restored as `479e26b7`) with the rejected working tree. These are all request-path differences found, including unchanged settings and remaining runtime limits:

| Property | Working old image | Rejected session build | This revision |
| --- | --- | --- | --- |
| URL / host / path | HTTPS `in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic` | Identical | Identical |
| Query values and serialization order | `eventCode,dateCode,isDesktop,regionCode,xLocationShared,memberId,lsId,subCode,lat,lon`, original values and urllib urlencode | Identical | Identical; frozen literal regression |
| Explicit headers, in order | User-Agent, Accept, x-app-code, x-region-code, x-region-slug, x-platform-code, x-location-selection | Added Accept-Language and Referer after Accept; subsequent headers shifted | Original seven headers and order restored |
| Header values | `BMSLocalNotifier/0.1`, `application/json`, `WEB`, `HYD`, `hyderabad`, `DESKTOP-WEB`, `manual` | Original values retained; added `en-IN,en;q=0.9` and public explore-page referrer | Original values only |
| Native header normalization / implicit headers | urllib Request and HTTPSHandler / http.client behavior, including connection close | Requests preparation / urllib3 behavior, persistent connection; different library-generated ordering/casing possible | Native urllib path restored, with no manual wire-header overrides |
| Cookies | No cookie jar, no retained response cookies, no Cookie header | In-memory Requests cookie jar replayed Cloudflare cookies | No jar or replay |
| Session / connection reuse | Separate native urlopen per read, observed Connection close | Thread-local Session and pooled HTTPS connections, observed keep-alive | Native behavior restored |
| Bootstrap | None | Production code had none; a public-page bootstrap was only a local comparison experiment | None |
| HTTP client | Python stdlib urllib.request + http.client | Requests 2.34.2 + urllib3; urllib3 version was not pinned in cloud requirements (local comparison used 2.8.0) | stdlib only; Requests dependency and packaged session module removed |
| TLS | stdlib verified default HTTPS context | Requests/urllib3 verified default HTTPS context and its default trust/handshake handling | stdlib defaults restored; no custom cipher/ALPN/fingerprint settings |
| HTTP/2 | Not enabled; native HTTP/1.1 | Not enabled; Requests HTTP/1.1 | Native HTTP/1.1 |
| Runtime version | Docker base `python:3.12-slim-bookworm` | Same base tag | Same base tag. Exact deployed Python patch/OpenSSL versions could not be read: Railway SSH has no configured key. No version equivalence beyond the unchanged base definition is asserted. |
| Timeout | native `urlopen(..., timeout=25)` | Requests `timeout=25` with its connect/read semantics | native timeout call restored |
| Redirect / adapter retries | Native urllib defaults | Explicit no redirects and adapter max_retries=0 | Native defaults restored |
| 403 cycle | Ends cycle, per-job backoff, normally 180 s / repeated 300 s | One additional fresh-session attempt at next 30 s slot, then backoff | Extra attempt, reset and retry-priority logic removed; old cycle behavior restored |
| Body / budget recording order | Native response metadata recorded before JSON body read | Session buffered body before returning response metadata | Native order restored |
| Response safety / diagnostics | Original parsing and safe refusal fields | Added malformed-shape guard and CF-Ray; session diagnostics | Malformed-shape guard, wrong-date discard, safe refusal diagnostics retained; they do not alter outgoing requests |
| Shared budget | 30-second listing floor and 120/hour; separate auxiliary lanes | All listing lanes share the floor/cap | Shared floor/cap retained; 403 Retry-After remains persisted safely |
| Job due / delivery | Response-based due and previous delivery delays | Corrected start-based due and immediate delivery | Those independent fixes retained |

The rejected session source is preserved for review at `runtime/rejected-bms-session.py`, not packaged or imported. Other uncommitted changes remain preserved.

Logging now splits streams: INFO/WARNING single-line JSON goes to stdout; ERROR/CRITICAL goes to stderr. A subprocess regression verifies both directions and that genuine errors are not downgraded.

### Tests and comparison rule before deployment

- **376 Python passed, 5 POSIX-only skips, 8 subtests passed**, 41.17 seconds; zero failures or test warnings. The count changed because obsolete session-reuse tests were replaced with native-contract tests.
- **27 JavaScript tests passed**, zero failures or warnings. `git diff --check` passes.
- The frozen contract test asserts the complete URL literal, query ordering, exact ordered header list, native urllib client invocation, GET/no body, no cookies, and `timeout=25`, both with and without the persistent budget enabled.
- The immediately preceding old-image log sample contains **28 HTTP 200s (24 usable and 4 wrong-date), 1 HTTP 403: 1/29 = 3.448%**. It spans approximately 14.3 minutes of old-image activity, within the requested preceding 15-minute query. Saved in `runtime/native-round-baseline.jsonl`.
- The candidate must have at least one HTTP 200 among its first four actual showtime requests. A crash triggers immediate rollback to `479e26b7`'s exact image. After 15 minutes, a 403 proportion higher than **1/29** triggers rollback rather than declaring success. This is an operational acceptance comparison, not a statistically conclusive estimate.

Native-client candidate **95ce29da-c40f-4d7a-a6fb-184670e4fa61** was uploaded from the working tree after those tests. Automatic rollback is enabled. It returned HTTP 200s among its initial checks, so the first-four gate passed; the 15-minute rate comparison is in progress.

Further comparison found a response-scheduling difference beyond the outgoing client contract: the working tree extended a repeated wrong-date job's retry to 300 seconds, while the archived image kept its 60-second failure retry. Wrong-date responses are still discarded in both cases. A subsequent one-line revision restores the old 60-second cadence while retaining INFO logging and safe discard; it is prepared locally and does **not** change candidate 95ce29da while that observation runs. The separate newer cross-movie 403 cooldown also remains a documented scheduling difference; it does not add network attempts and did not trigger on the two observed refusals for the same film.

**Round 1 was rolled back early:** the last saved observation contained 15 HTTP 200s, 2 HTTP 403s and one discarded-date response, with fresh heartbeats, unchanged urgent settings, an empty delivery queue and no issues. With a maximum of 31 attempts in a 15-minute window at the 30-second floor, two refusals already imply at least 6.45%, above the predeclared 3.448% gate. The candidate could not recover to the acceptance rate even if every remaining attempt succeeded. Saved measurements are in `runtime/native-round-one/`. No crash occurred; this rollback followed the owner's stricter rate criterion.

**Round 2** is candidate **afc94928-4825-4127-be41-9cd04db5e56f**. Its only source-code change relative to round 1 is restoring the old repeated-wrong-date retry cadence (60 seconds). The payload is still discarded and logged at INFO. The associated regression expectation was updated to the archived behavior. Both suites were rerun: **376 Python passed, 5 POSIX skips, 8 subtests passed in 43.02 s; 27 JavaScript passed**, zero failures or test warnings. The same first-four/crash/rate rollback gates are active; observation is in progress.

### Round 2 accepted — final live result

**afc94928-4825-4127-be41-9cd04db5e56f remains live.** Its automatic watcher completed **908.383 seconds** (over 15 minutes) without invoking rollback.

| Metric | Old-image comparison | Accepted native-client round 2 |
| --- | --- | --- |
| Source attempts | 29 | **31** |
| HTTP 200 | 28 | **30** |
| HTTP 403 | 1 | **1** |
| 403 proportion | **3.448%** | **3.226%** |
| Wrong-date payloads among HTTP 200 | 4 | **5**, discarded at INFO |
| Usable showtime responses | 24 | **25** |
| Crashes / genuine error-level lines | No crashes observed in baseline | **0 / 0** |
| Warning-level records | Old stderr presentation | **1**, the counted 403 warning |

The first four actual source checks were **200, 200, 200, 403**, passing the automatic startup gate. Request-start gaps were **30.028366–30.042963 seconds**, mean **30.034043 seconds**, with **zero gaps below 30 seconds**. The candidate's measured refusal proportion did not exceed the predeclared old-image gate, so it was retained. These short samples meet the operational criterion; they do not establish a statistically significant improvement or promise no future refusals.

At the final snapshot the urgent alert was active with full preference equality, heartbeat age **2.12 seconds**, zero pending messages and no health issues. An independent final GET again confirmed the original December 17 ALLU Dolby settings unchanged. Telegram remained configured throughout; no synthetic booking alert was sent.

All original delivery-speed, queue, retry-deadline, date-discard, 30-second floor and shared 120/hour fixes remain in the accepted working tree. The Requests transport, added language/referrer headers, cookie replay, pooling and extra fresh-session retry are absent. INFO/WARNING logs go to stdout; genuine ERROR/CRITICAL logs go to stderr, verified by subprocess tests.

Final measurements and all per-request gaps: `runtime/native-round-two/result.json`; request history: `runtime/native-round-two/requests.json`; deployment logs: `runtime/native-round-two/logs.jsonl`; live deployment metadata: `runtime/native-final-deployment.json`. The rejected round 1 remains documented separately under `runtime/native-round-one/`.

**No Git commit or push was performed. Existing uncommitted changes were preserved.** This successful native-client round supersedes the earlier failed session-based deployment and its rollback; the earlier sections are retained as the audit history.
