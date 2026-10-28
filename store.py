import hashlib
import json
import sqlite3
import time


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,url TEXT,payload TEXT,
      due REAL DEFAULT 0,checked REAL DEFAULT 0,expires REAL DEFAULT 0,error TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS job_due ON jobs(kind,due);
    CREATE TABLE IF NOT EXISTS venues(code TEXT PRIMARY KEY,name TEXT,url TEXT,initialized INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS inventory(url TEXT PRIMARY KEY,title TEXT,seen REAL);
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,payload TEXT,cutoff REAL,active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,payload TEXT);
    CREATE TABLE IF NOT EXISTS check_stats(source TEXT,job_key TEXT,attempts INTEGER DEFAULT 0,
      successes INTEGER DEFAULT 0,failures INTEGER DEFAULT 0,last_attempt REAL DEFAULT 0,
      last_success REAL DEFAULT 0,last_interval REAL DEFAULT 0,max_interval REAL DEFAULT 0,
      latency REAL DEFAULT 0,error TEXT DEFAULT '',PRIMARY KEY(source,job_key));
    CREATE TABLE IF NOT EXISTS metric_history(at INTEGER PRIMARY KEY,movies INTEGER,venues INTEGER,sessions INTEGER);
    CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,text TEXT,photo BLOB,
      created REAL,delivered REAL DEFAULT 0,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0);
    ''')
    if 'alert_id' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
        try:db.execute("ALTER TABLE outbox ADD COLUMN alert_id TEXT DEFAULT ''")
        except sqlite3.OperationalError as error:
            if 'duplicate column name' not in str(error):raise
    return db


def meta(db, key, default=''):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def setmeta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))


def enqueue(db, kind, url, payload, expires=0):
    key = kind + ':' + url
    db.execute('''INSERT INTO jobs(id,kind,url,payload,expires) VALUES(?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,expires=excluded.expires''',
               (key, kind, url, json.dumps(payload), expires))


def emit(db, event, text, photo=None, alert_id=''):
    key = hashlib.sha256(event.encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO outbox(id,text,photo,created,alert_id) VALUES(?,?,?,?,?)',
               (key, text, photo, time.time(), alert_id))


def snapshot(db, key, current, message, photo=None, baseline=False):
    row = db.execute('SELECT payload FROM snapshots WHERE id=?', (key,)).fetchone()
    previous = json.loads(row[0]) if row else None
    added = sorted(set(current) - set(previous or {}))
    revision = int(meta(db, 'revision:'+key, '0'))
    if previous != current:
        revision += 1
        setmeta(db, 'revision:'+key, revision)
    if previous is not None and added and not baseline:
        # A seat can become unavailable then available repeatedly.
        event = key + ':' + str(revision)
        emit(db, event, message(added), photo)
    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)', (key, json.dumps(current)))
    return added if previous is not None else []


def coverage(db):
    now = time.time()
    result = {'venues': db.execute('SELECT count(*) FROM venues').fetchone()[0],
              'movies': db.execute('SELECT count(*) FROM inventory').fetchone()[0],
              'sessions': db.execute('SELECT count(*) FROM sessions WHERE active=1 AND cutoff>?', (now,)).fetchone()[0],
              'pending_alerts': db.execute('SELECT count(*) FROM outbox WHERE delivered=0').fetchone()[0]}
    for kind in ('catalog', 'movie', 'discovery', 'venue', 'seat'):
        row = db.execute('''SELECT count(*),sum(checked>0),sum(error!=''),min(NULLIF(checked,0))
        FROM jobs WHERE kind=? AND (expires=0 OR expires>?)''', (kind, now)).fetchone()
        result[kind] = {'total': row[0], 'checked': row[1] or 0, 'errors': row[2] or 0,
                        'oldest_check_age': round(now-row[3]) if row[3] else None}
    return result


def record_check(db,source,key,started,error='',now=None):
    now=time.time() if now is None else now
    previous=db.execute('SELECT last_success FROM check_stats WHERE source=? AND job_key=?',(source,key)).fetchone()
    interval=max(0,now-previous[0]) if previous and previous[0] and not error else 0
    db.execute("""INSERT INTO check_stats VALUES(?,?,1,?,?,?, ?,?,?,?,?)
      ON CONFLICT(source,job_key) DO UPDATE SET attempts=attempts+1,
      successes=successes+excluded.successes,failures=failures+excluded.failures,
      last_attempt=excluded.last_attempt,
      last_success=CASE WHEN excluded.successes=1 THEN excluded.last_success ELSE last_success END,
      last_interval=CASE WHEN excluded.last_interval>0 THEN excluded.last_interval ELSE last_interval END,
      max_interval=max(max_interval,excluded.max_interval),latency=excluded.latency,error=excluded.error""",
      (source,key,int(not error),int(bool(error)),now,now if not error else 0,interval,interval,max(0,now-started),error[:200]))
    db.execute('DELETE FROM check_stats WHERE last_attempt<?',(now-30*86400,))
