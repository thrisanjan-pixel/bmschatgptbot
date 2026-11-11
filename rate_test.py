"""A bounded maintenance experiment using the existing seat request budget."""
import json
import time
from datetime import datetime, timedelta, timezone
from html import escape

from store import emit, maintenance_active, meta, setmeta

DAY = 86400
HOUR = 3600
PACE = (60, 30, 20, 10)
HOST = 'services-in.bookmyshow.com'
IST = timezone(timedelta(hours=5, minutes=30))


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS rate_test_samples(
        run TEXT, at REAL, pace INTEGER, status INTEGER, outcome TEXT,
        retry_after TEXT, rate_headers TEXT)''')


def start(db, run, now=None):
    from http_source import schema as source_schema
    now = time.time() if now is None else now
    schema(db)
    source_schema(db)
    if meta(db, 'rate_test_id') == run:
        return
    for key, value in {'id': run, 'start': now, 'end': now + DAY, 'due': now,
                       'show': '', 'failures': 0, 'incident': '', 'safe_gap': 0}.items():
        setmeta(db, 'rate_test_' + key, value)
    db.commit()


def test_gap(db, now):
    if not maintenance_active(db, now):
        return None
    elapsed = max(0, now - float(meta(db, 'rate_test_start', '0')))
    planned = PACE[min(int(elapsed // HOUR), len(PACE) - 1)]
    return max(planned, int(meta(db, 'rate_test_safe_gap', '0')))


def test_show(db, now):
    show_id = meta(db, 'rate_test_show')
    row = db.execute('SELECT id,payload FROM sessions WHERE id=? AND active=1 AND cutoff>?', (show_id, now+180)).fetchone()
    if not row:
        row = db.execute('''SELECT s.id,s.payload FROM sessions s
            LEFT JOIN row_layouts r ON r.show_id=s.id
            WHERE s.active=1 AND s.cutoff>?
            ORDER BY (COALESCE(r.checked,0)>0 AND r.error='') DESC, s.cutoff LIMIT 1''', (now+180,)).fetchone()
    if not row:
        return None
    setmeta(db, 'rate_test_show', row['id'])
    return json.loads(row['payload'])


def step(db, now):
    from cloud_row_reader import read
    from http_source import SourceWait, schema as source_schema

    setmeta(db, 'row_reader_heartbeat', now)
    pace = test_gap(db, now)
    if pace is None or float(meta(db, 'rate_test_due', '0')) > now:
        db.commit()
        return
    source_schema(db)
    host = db.execute('SELECT not_before FROM source_hosts WHERE host=?', (HOST,)).fetchone()
    wait = max(host[0] if host else 0, float(meta(db, 'seat_cooldown_until', '0')),
               float(meta(db, 'seat_next_request_at', '0')))
    if wait > now:
        setmeta(db, 'rate_test_due', wait)
        db.commit()
        return
    show = test_show(db, now)
    due = now+pace
    setmeta(db, 'rate_test_due', due if show else now+60)
    db.commit()
    if not show:
        return
    retry_header = ''
    try:
        read(show)
        status, outcome = 200, 'ok'
    except SourceWait as error:
        # Let the shared request budget and existing upstream cooldown dictate the next attempt.
        setmeta(db, 'rate_test_due', max(now+1, error.until))
        db.commit()
        return
    except Exception as error:
        status = getattr(error, 'status', 0)
        retry_header = str(getattr(error, 'diagnostic', {}).get('headers', {}).get('Retry-After', ''))
        outcome = (str(error).split('\n', 1)[0] or type(error).__name__)[:160]
        # Maintain the production backoff for refusals and other source errors.
        retry = getattr(error, 'retry', 0) or 0
        retry_at = time.time()+max(300, retry)
        setmeta(db, 'rate_test_due', max(due, retry_at))
        if status == 429:
            setmeta(db, 'seat_cooldown_until', retry_at)
            setmeta(db, 'seat_last_rate_limit', time.time())
            setmeta(db, 'rate_test_safe_gap', PACE[max(0, PACE.index(pace)-1)])
    logged = db.execute('''SELECT retry_after,rate_headers FROM source_requests
        WHERE host=? AND at>=? ORDER BY at DESC LIMIT 1''', (HOST, now)).fetchone()
    retry_after, headers = tuple(logged) if logged else ('','{}')
    retry_after = retry_after or retry_header
    run = meta(db, 'rate_test_id')
    db.execute('INSERT INTO rate_test_samples VALUES(?,?,?,?,?,?,?)', (run, now, pace, status, outcome, retry_after, headers))
    failures = 0 if outcome == 'ok' else int(meta(db, 'rate_test_failures', '0'))+1
    setmeta(db, 'rate_test_failures', failures)
    incident = '429' if status == 429 else 'errors' if failures >= 3 else ''
    if outcome == 'ok':
        setmeta(db, 'rate_test_incident', '')
    elif incident and incident != meta(db, 'rate_test_incident'):
        setmeta(db, 'rate_test_incident', incident)
        label = 'BookMyShow returned HTTP 429' if status == 429 else f'{failures} consecutive seat checks failed'
        text = f'Screenwatch rate test: {label} during the {pace}-second test.\n{outcome}'
        count = db.execute('SELECT count(*) FROM source_requests WHERE host=? AND at>?', (HOST, now-HOUR)).fetchone()[0]
        text += f'\nSeat requests observed in the previous hour: {count} (not an upstream quota).'
        if retry_after:
            text += f'\nRetry-After: {retry_after}'
        if headers != '{}':
            text += f'\nReported headers: {headers}'
        text += '\nReport: https://bmschatgptbot-production.up.railway.app/maintenance'
        emit(db, f'rate-test:{run}:{now}', text, alert_id='__rate_test__')
    db.commit()


def report(db, now=None):
    now = time.time() if now is None else now
    run = meta(db, 'rate_test_id')
    started = float(meta(db, 'rate_test_start', '0'))
    end = float(meta(db, 'rate_test_end', '0'))
    active = bool(run and now < end)
    samples = db.execute('SELECT * FROM rate_test_samples WHERE run=? ORDER BY at', (run,)).fetchall() if run else []
    intervals = []
    for pace in sorted(PACE):
        rows = [row for row in samples if row['pace'] == pace]
        gaps = [b['at']-a['at'] for a,b in zip(rows,rows[1:])]
        intervals.append({'seconds': pace, 'attempts': len(rows), 'successes': sum(row['outcome']=='ok' for row in rows),
                          'limited': sum(row['status']==429 for row in rows), 'fastest_gap': round(min(gaps),1) if gaps else None})
    evidence = [dict(row) for row in samples if row['status']==429 or row['rate_headers']!='{}'][-12:]
    return {'active': active, 'start': started, 'end': end, 'current_interval': test_gap(db, now) if active else None,
            'intervals': intervals, 'evidence': evidence}


def page(data):
    def ist(timestamp):
        return datetime.fromtimestamp(timestamp, IST).strftime('%d %b, %I:%M %p IST')
    title = 'Screenwatch maintenance' if data['active'] else 'Screenwatch rate test'
    timing = f"{ist(data['start'])} to {ist(data['end'])}" if data['start'] else 'No test has run.'
    status = f"Currently testing {data['current_interval']}-second spacing." if data['active'] else 'The dashboard is available.'
    rows = ''.join(f"<tr><td>{item['seconds']} s</td><td>{item['attempts']}</td><td>{item['successes']}</td><td>{item['limited']}</td><td>{str(item['fastest_gap'])+' s' if item['fastest_gap'] is not None else '—'}</td></tr>" for item in data['intervals'])
    evidence = ''.join(f"<li>{ist(row['at'])} · {row['pace']} s · HTTP {row['status']} · {escape(row['retry_after'] or 'No Retry-After')} · {escape(row['rate_headers'])}</li>" for row in data['evidence'])
    refresh = '<meta http-equiv="refresh" content="30">' if data['active'] else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>{title}</title>
<style>body{{background:#08090c;color:#f3f4f6;font:16px system-ui;margin:0;padding:32px 18px}}main{{max-width:720px;margin:8vh auto}}h1{{letter-spacing:-.04em}}p,li{{color:#adb4c1;line-height:1.5}}table{{width:100%;border-collapse:collapse;background:#101217;border:1px solid #272b33}}td,th{{text-align:left;padding:12px 8px;border-bottom:1px solid #272b33}}a{{color:#a5d7ff}}li{{overflow-wrap:anywhere}}</style>
<main><h1>{title}</h1><p>{timing}</p><p>{status} Test notices go to Telegram if the source starts rejecting checks.</p>
<table><thead><tr><th>Interval</th><th>Checks</th><th>Successful</th><th>HTTP 429</th><th>Shortest gap</th></tr></thead><tbody>{rows}</tbody></table>
<p>The test steps through 60, 30, 20 and 10 seconds. After a 429 it honors the retry deadline and backs off. A run without quota headers measures the tested pace and any observed refusals.</p>
<ul>{evidence}</ul><a href="{'/maintenance' if data['active'] else '/'}">{'Refresh report' if data['active'] else 'Open dashboard'}</a></main></html>'''
