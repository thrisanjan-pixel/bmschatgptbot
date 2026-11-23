"""Hot-standby failover: heartbeat answers, promotion with outbox merge, demotion,
standby alert-config sync, Healthchecks.io pings and Litestream status.

Nothing here sends to Telegram. Promotion only flips the runtime role; the existing
senders (api_watch and health_watch through telegram.drain) start on their next loop.
"""
import hmac
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import role
from store import DEDUPE_WINDOW, connect, duplicate_of, meta, setmeta

PROMOTE_LOCK = threading.Lock()


def begin(db):
    """Start a write transaction, finishing any implicit one first."""
    if db.in_transaction:
        db.commit()
    db.execute('BEGIN IMMEDIATE')


def env_number(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def fresh_window():
    """Seconds an undelivered alert stays worth sending after a promotion (PROMOTE_FRESH_SECONDS, default 900)."""
    return max(60.0, env_number('PROMOTE_FRESH_SECONDS', 900))


# ---------------------------------------------------------------- auth

def secret_ok(header):
    """Constant-time check of 'Authorization: Bearer <HEARTBEAT_SECRET>'. An unset or short secret refuses all."""
    secret = os.environ.get('HEARTBEAT_SECRET', '')
    if len(secret) < 32:
        return False
    return hmac.compare_digest((header or '').encode(), ('Bearer ' + secret).encode())


# ---------------------------------------------------------------- telemetry

def last_bms_success(db):
    """Newest successful BookMyShow check of any kind (showtimes or seats)."""
    listing = float(meta(db, 'api_last_success', '0') or 0)
    try:
        seats = db.execute("SELECT coalesce(max(last_success),0) FROM check_stats WHERE source='rows'").fetchone()[0]
    except sqlite3.Error:
        seats = 0
    return max(listing, float(seats or 0))


def outbox_depth(db):
    return db.execute('SELECT count(*) FROM outbox WHERE delivered=0').fetchone()[0]


def dead_letters(db):
    try:
        return db.execute("SELECT count(*) FROM suppressed_outbox").fetchone()[0]
    except sqlite3.Error:
        return 0


def litestream_status():
    """What is known about Litestream replication without touching R2.

    configured: bucket and keys are set. wrapped: this process was started by
    `litestream replicate -exec`. Metrics come from Litestream's local /metrics."""
    configured = bool(os.environ.get('LITESTREAM_BUCKET') and os.environ.get('LITESTREAM_ACCESS_KEY_ID')
                      and os.environ.get('LITESTREAM_SECRET_ACCESS_KEY'))
    result = {'configured': configured, 'wrapped': os.environ.get('LITESTREAM_WRAPPED') == '1',
              'replica_path': os.environ.get('LITESTREAM_REPLICA_PATH', ''), 'metrics': None}
    if not result['wrapped']:
        return result
    try:
        with urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=1) as response:
            text = response.read(500_000).decode('utf-8', 'replace')
    except Exception:
        return result
    metrics = {}
    for line in text.splitlines():
        if line.startswith('litestream_') and not line.startswith('#'):
            name, _, value = line.rpartition(' ')
            key = name.split('{', 1)[0]
            try:
                metrics[key] = metrics.get(key, 0) + float(value)
            except ValueError:
                pass
    result['metrics'] = {k: metrics[k] for k in sorted(metrics)[:20]}
    return result


def snapshot(db):
    """This instance's failover status, used by /heartbeat, /readyz and the dashboard."""
    role_info = role.describe(db)
    try:
        watchdog = json.loads(meta(db, 'watchdog_view', '{}'))
    except ValueError:
        watchdog = {}
    return {**role_info,
            'last_bms_success': last_bms_success(db),
            'outbox_depth': outbox_depth(db),
            'last_telegram_success_at': float(meta(db, 'last_telegram_success_at', '0') or 0),
            'last_failed_send_at': float(meta(db, 'last_failed_send_at', '0') or 0),
            'dead_letters': dead_letters(db),
            'ambiguous_sends': int(meta(db, 'ambiguous_sends', '0') or 0),
            'dedupe_skipped': int(meta(db, 'dedupe_skipped', '0') or 0),
            'watchdog_last_seen': float(meta(db, 'watchdog_last_seen', '0') or 0),
            'watchdog': watchdog if isinstance(watchdog, dict) else {},
            'last_promotion': json.loads(meta(db, 'last_promotion', '{}') or '{}'),
            'healthchecks_last_ping': float(meta(db, 'healthchecks_last_ping', '0') or 0),
            'healthchecks_configured': bool(os.environ.get('HEALTHCHECKS_URL')),
            'stale_sources': json.loads(meta(db, 'stale_sources', '{}') or '{}'),
            'standby_rate_factor': role.standby_scale(db),
            'last_config_sync': float(meta(db, 'last_config_sync', '0') or 0),
            'litestream': litestream_status(),
            'version': os.environ.get('RAILWAY_GIT_COMMIT_SHA', os.environ.get('GIT_SHA', ''))[:12]}


def heartbeat(db, body=None):
    """Record a watchdog ping (and the watchdog's view of both instances); return our status."""
    now = time.time()
    setmeta(db, 'watchdog_last_seen', now)
    if isinstance(body, dict) and isinstance(body.get('instances'), dict):
        view = {}
        for name in ('primary', 'standby'):
            item = body['instances'].get(name)
            if isinstance(item, dict):
                view[name] = {k: item.get(k) for k in ('last_seen', 'role', 'ok', 'missed_since') if k in item}
        setmeta(db, 'watchdog_view', json.dumps({'at': now, 'instances': view}))
    db.commit()
    result = snapshot(db)
    db.commit()
    return result


# ---------------------------------------------------------------- replica restore and merge

def litestream_config(prefix, out_dir):
    """A temporary Litestream config whose one database reads the given replica prefix in R2."""
    db_path = str(Path(out_dir) / 'source.sqlite3')
    config = Path(out_dir) / 'restore.yml'
    # Values come from the environment at run time (Litestream expands ${VAR}); no secret is written here.
    config.write_text(
        'dbs:\n'
        f'  - path: {db_path}\n'
        '    replicas:\n'
        '      - type: s3\n'
        '        bucket: ${LITESTREAM_BUCKET}\n'
        f'        path: {prefix}\n'
        '        endpoint: ${LITESTREAM_ENDPOINT}\n'
        '        region: auto\n'
        '        force-path-style: true\n'
        '        access-key-id: ${LITESTREAM_ACCESS_KEY_ID}\n'
        '        secret-access-key: ${LITESTREAM_SECRET_ACCESS_KEY}\n', encoding='utf-8')
    return str(config), db_path


def restore_replica(prefix='primary', timeout=90):
    """Restore the newest replica under s3://$LITESTREAM_BUCKET/<prefix> to a temp file.

    Returns the restored file path, or None when Litestream or R2 is not configured or
    no replica exists. Raises RuntimeError when the restore itself failed."""
    if not litestream_status()['configured'] or not shutil.which('litestream'):
        return None
    folder = tempfile.mkdtemp(prefix='screenwatch-replica-')
    config, db_path = litestream_config(prefix, folder)
    out = str(Path(folder) / 'restored.sqlite3')
    run = subprocess.run(['litestream', 'restore', '-config', config, '-if-replica-exists', '-o', out, db_path],
                         capture_output=True, text=True, timeout=timeout)
    if run.returncode:
        # Litestream's own message names no credential, but keep it short.
        raise RuntimeError('litestream restore failed: ' + (run.stderr or run.stdout).strip().splitlines()[-1][:200]
                           if (run.stderr or run.stdout).strip() else 'litestream restore failed')
    return out if Path(out).exists() else None


def merge_outbox(db, replica_path, now=None):
    """Bring the other instance's outbox into ours without sending anything twice.

    - A row the replica shows delivered marks our copy (same id, or same dedupe key) delivered.
    - An undelivered replica row newer than fresh_window() is added, unless we already hold it
      (same id, or same alert + SHOWID:ROWSET within DEDUPE_WINDOW).
    - Older undelivered replica rows are dropped: they are stale.
    Returns counts."""
    now = time.time() if now is None else now
    window = fresh_window()
    counts = {'added': 0, 'marked_delivered': 0, 'already_had': 0, 'stale_skipped': 0}
    source = sqlite3.connect(Path(replica_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    source.row_factory = sqlite3.Row
    try:
        columns = {r[1] for r in source.execute('PRAGMA table_info(outbox)')}
        if not columns:
            return counts
        pick = ', '.join(c if c in columns else ("''" if c in ('alert_id', 'dedupe') else '0') + ' AS ' + c
                         for c in ('id', 'text', 'photo', 'created', 'delivered', 'alert_id', 'dedupe', 'release_ts'))
        rows = source.execute(f'SELECT {pick} FROM outbox WHERE delivered>0 OR created>?', (now - window,)).fetchall()
        stale = source.execute('SELECT count(*) FROM outbox WHERE delivered=0 AND created<=?', (now - window,)).fetchone()[0]
    finally:
        source.close()
    counts['stale_skipped'] = stale
    begin(db)
    try:
        for row in rows:
            if row['delivered'] and row['delivered'] > 0:
                changed = db.execute('UPDATE outbox SET delivered=? WHERE id=? AND delivered=0', (row['delivered'], row['id'])).rowcount
                if row['dedupe']:
                    changed += db.execute('UPDATE outbox SET delivered=? WHERE delivered=0 AND alert_id=? AND dedupe=? AND abs(release_ts-?)<=?',
                                          (row['delivered'], row['alert_id'], row['dedupe'], row['release_ts'], DEDUPE_WINDOW)).rowcount
                counts['marked_delivered'] += changed
                continue
            if db.execute('SELECT 1 FROM outbox WHERE id=?', (row['id'],)).fetchone() or \
                    duplicate_of(db, row['alert_id'], row['dedupe'], row['release_ts']):
                counts['already_had'] += 1
                continue
            db.execute('INSERT INTO outbox(id,text,photo,created,alert_id,dedupe,release_ts) VALUES(?,?,?,?,?,?,?)',
                       (row['id'], row['text'], row['photo'], row['created'], row['alert_id'], row['dedupe'], row['release_ts']))
            counts['added'] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts


def suppress_stale(db, now=None):
    """Archive our own undelivered rows older than fresh_window(), so a promoted standby
    does not flood Telegram with alerts that queued while its sender was off."""
    now = time.time() if now is None else now
    from watch_preferences import archive_message
    ids = [r[0] for r in db.execute('SELECT id FROM outbox WHERE delivered=0 AND created<=?', (now - fresh_window(),))]
    begin(db)
    try:
        for message_id in ids:
            archive_message(db, message_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(ids)


def promote(path, restore=None, reason='watchdog'):
    """Become the active sender. Idempotent: a second call while active changes nothing.

    Order: restore the primary's replica, merge its outbox, drop stale rows, then flip the
    role. A failed restore never blocks promotion: alerts matter more than a perfect merge."""
    restore = restore or restore_replica
    with PROMOTE_LOCK:
        db = connect(path)
        try:
            current = role.runtime_role(db)
            db.commit()
            if current == role.ACTIVE:
                return {'role': role.ACTIVE, 'changed': False}
            result = {'role': role.ACTIVE, 'changed': True, 'at': time.time(), 'reason': reason[:100]}
            try:
                replica = restore(os.environ.get('PROMOTE_FROM_PREFIX', 'primary'))
                if replica:
                    try:
                        result['merge'] = merge_outbox(db, replica)
                    finally:
                        shutil.rmtree(Path(replica).parent, ignore_errors=True)
                else:
                    result['merge'] = 'no replica (Litestream not configured or nothing in R2)'
            except Exception as error:
                logging.warning('Promotion merge skipped: %s', error)
                result['merge'] = 'failed: ' + str(error)[:200]
            result['stale_suppressed'] = suppress_stale(db)
            begin(db)
            role.set_role(db, role.ACTIVE, 'promoted: ' + reason)
            setmeta(db, 'last_promotion', json.dumps(result))
            db.commit()
            logging.warning('Promoted to active: %s', json.dumps(result))
            return result
        finally:
            db.close()


def demote(path, reason='manual failback'):
    """Stop sending (manual failback). Idempotent. Queued rows stay for inspection."""
    with PROMOTE_LOCK:
        db = connect(path)
        try:
            begin(db)
            changed = role.set_role(db, role.STANDBY, 'demoted: ' + reason)
            db.commit()
            return {'role': role.STANDBY, 'changed': changed}
        finally:
            db.close()


# ---------------------------------------------------------------- standby config sync

SYNC_META = ('notifications_paused', 'telegram_enabled', 'stale_warning_enabled', 'stale_warning_minutes')


def sync_config(db, replica_path):
    """Make a standby watch exactly what the primary watches: copy the alerts table and the
    alert switches from the primary's replica. Seat and listing state stay our own."""
    import watch_preferences as prefs
    prefs.ensure(db)
    source = sqlite3.connect(Path(replica_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    source.row_factory = sqlite3.Row
    try:
        if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alerts'").fetchone():
            return 0
        theirs = {r[1] for r in source.execute('PRAGMA table_info(alerts)')}
        ours = [r[1] for r in db.execute('PRAGMA table_info(alerts)')]
        columns = [c for c in ours if c in theirs]
        rows = source.execute('SELECT ' + ','.join(columns) + ' FROM alerts').fetchall()
        switches = {k: v for k, v in source.execute(
            'SELECT key,value FROM meta WHERE key IN (' + ','.join('?' * len(SYNC_META)) + ')', SYNC_META)}
    finally:
        source.close()
    begin(db)
    try:
        ids = [r['id'] for r in rows]
        db.execute('DELETE FROM alerts WHERE id NOT IN (' + (','.join('?' * len(ids)) or "''") + ')', ids)
        for r in rows:
            db.execute('INSERT OR REPLACE INTO alerts(' + ','.join(columns) + ') VALUES(' + ','.join('?' * len(columns)) + ')',
                       [r[c] for c in columns])
        for key, value in switches.items():
            setmeta(db, key, value)
        setmeta(db, 'alerts_migration_complete', '1')
        setmeta(db, 'last_config_sync', time.time())
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(rows)


def maybe_sync_config(db, restore=None, now=None):
    """Called from the health loop: every STANDBY_SYNC_SECONDS (default 300) while standby."""
    now = time.time() if now is None else now
    if role.runtime_role(db) != role.STANDBY:
        return None
    if now - float(meta(db, 'last_config_sync_attempt', '0') or 0) < max(60, env_number('STANDBY_SYNC_SECONDS', 300)):
        return None
    setmeta(db, 'last_config_sync_attempt', now)
    db.commit()
    replica = (restore or restore_replica)(os.environ.get('PROMOTE_FROM_PREFIX', 'primary'))
    if not replica:
        return None
    try:
        return sync_config(db, replica)
    finally:
        shutil.rmtree(Path(replica).parent, ignore_errors=True)


# ---------------------------------------------------------------- Healthchecks.io

def ping_healthchecks(db, now=None, send=None):
    """Ping HEALTHCHECKS_URL after a successful BookMyShow check, at most once a minute.

    Runs the HTTP call on a background thread so a slow ping never delays a check.
    Returns True when a ping was started."""
    url = (os.environ.get('HEALTHCHECKS_URL') or '').strip()
    if not url.startswith('https://'):
        return False
    now = time.time() if now is None else now
    if now - float(meta(db, 'healthchecks_last_ping', '0') or 0) < 60:
        return False
    setmeta(db, 'healthchecks_last_ping', now)

    def ping():
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method='GET'), timeout=10):
                pass
        except Exception as error:
            logging.warning('Healthchecks ping failed: %s', type(error).__name__)

    (send or (lambda: threading.Thread(target=ping, daemon=True).start()))()
    return True


# ---------------------------------------------------------------- staleness detector

def stale_factor():
    return max(2.0, env_number('BMS_STALE_FACTOR', 3))


def observe_payload(db, source, key, payload, expected_refresh, now=None):
    """Track how long a source payload has stayed byte-identical.

    When it has not changed for stale_factor() x expected_refresh seconds it is marked
    suspect (a cached or frozen response), logged, and escalated once per episode to
    the dashboard and ntfy. Returns True while suspect. expected_refresh<=0 disables it."""
    if expected_refresh <= 0:
        return False
    import hashlib
    now = time.time() if now is None else now
    digest = hashlib.sha256(payload if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    name = f'payload_seen:{source}:{key}'
    try:
        seen = json.loads(meta(db, name, '{}'))
    except ValueError:
        seen = {}
    if seen.get('hash') != digest:
        setmeta(db, name, json.dumps({'hash': digest, 'since': now}))
        clear_stale(db, source, key)
        return False
    age = now - float(seen.get('since', now))
    if age < stale_factor() * expected_refresh:
        return False
    stale = json.loads(meta(db, 'stale_sources', '{}') or '{}')
    label = f'{source}:{key}'
    if label not in stale:
        stale[label] = {'since': seen.get('since'), 'flagged': now}
        setmeta(db, 'stale_sources', json.dumps(stale))
        logging.warning('Source payload for %s unchanged for %d s (expected refresh %d s): suspect stale', label, age, expected_refresh)
        import ntfy
        threading.Thread(target=ntfy.notify, args=(f'Screenwatch: {label} has returned the same data for {int(age // 60)} min. '
                                                   'It may be a cached or frozen response.',), kwargs={'title': 'Screenwatch stale source'},
                         daemon=True).start()
    return True


def clear_stale(db, source, key):
    stale = json.loads(meta(db, 'stale_sources', '{}') or '{}')
    if stale.pop(f'{source}:{key}', None) is not None:
        setmeta(db, 'stale_sources', json.dumps(stale))
