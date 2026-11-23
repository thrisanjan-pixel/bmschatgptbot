"""Home seat worker: reads BookMyShow seat layouts from a home connection for the server.

The server (BMS_SEAT_SOURCE=worker) keeps the alerts, the listings and Telegram. This
worker asks it for the next due seat read, reads the layout from this PC with the same
request budget, backoff and circuit breaker the server uses, and posts the result back.

    set SCREENWATCH_URL=https://your-app.up.railway.app
    set BMS_WORKER_TOKEN=<same long random value as on the server>
    python home_worker.py

The local budget database (BMS_DB, default home_worker.sqlite3 next to this file) keeps
the spacing, 429 deadlines and backoff across restarts of the worker.
"""
import json
import os
import signal
import time
import urllib.request
from pathlib import Path

IDLE_SECONDS = 5


def call(base, token, path, payload, timeout=30):
    request = urllib.request.Request(base.rstrip('/') + path, json.dumps(payload).encode(),
        {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': 'ScreenwatchHomeWorker/1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def failure(error):
    """What the server needs to schedule the retry. Never response bodies or cookies."""
    from http_source import SourceWait
    if isinstance(error, SourceWait):
        return {'wait_until': error.until}
    return {'message': (str(error).partition('\n')[0] or type(error).__name__)[:200],
            'status': getattr(error, 'status', 0) or 0, 'retry': getattr(error, 'retry', 0) or 0,
            'sold_out': bool(getattr(error, 'sold_out', False))}


def run_once(base, token):
    """Claim and read one seat layout. Returns True when there was work."""
    job = call(base, token, '/api/worker/claim', {}).get('job')
    if not job:
        return False
    from cloud_row_reader import read
    try:
        report = {'show_id': job['show_id'], 'data': read(job['show'], manual=bool(job.get('manual')))}
    except Exception as error:
        report = {'show_id': job['show_id'], 'error': failure(error)}
    # If the server is unreachable here, the lease lapses and the read is handed out again.
    call(base, token, '/api/worker/result', report)
    return True


def main():
    base = os.environ.get('SCREENWATCH_URL', '')
    token = os.environ.get('BMS_WORKER_TOKEN', '')
    if not base.startswith('https://') and not base.startswith('http://127.0.0.1') and not base.startswith('http://localhost'):
        raise SystemExit('Set SCREENWATCH_URL to the server address (https://...).')
    if len(token) < 32:
        raise SystemExit('Set BMS_WORKER_TOKEN to the same value as on the server (32+ characters).')
    os.environ['BMS_REQUEST_BUDGET'] = '1'
    os.environ.setdefault('BMS_DB', str(Path(__file__).with_name('home_worker.sqlite3')))
    stop = False

    def halt(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, halt)
    signal.signal(signal.SIGTERM, halt)
    print('Home seat worker running. Press Ctrl+C to stop.', flush=True)
    while not stop:
        try:
            busy = run_once(base, token)
        except Exception as error:
            print(f'home_worker: {type(error).__name__}: {str(error)[:120]}; retrying in 30 seconds', flush=True)
            time.sleep(30)
            continue
        if not busy:
            time.sleep(IDLE_SECONDS)


if __name__ == '__main__':
    main()
