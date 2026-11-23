"""Persistent source request budgets and safe HTTP outcome logging."""
import math,os,time,urllib.request,urllib.error
from urllib.parse import urlsplit
from store import connect

class SourceWait(RuntimeError):
    def __init__(self,until):
        self.until=until
        super().__init__('Waiting for the permitted source request budget')


POLICIES={'services-in.bookmyshow.com':(60,60),'in.bookmyshow.com':(60,60),'in-cdn.bmscdn.com':(6,600)}
# Seat loads someone asked for on the page: a small separate budget, so they run at once
# without taking slots from alert checks. Source cooldowns (429) still apply.
MANUAL={'services-in.bookmyshow.com':(10,10),'in.bookmyshow.com':(10,10)}
# Seat-category lookups for alert seat reads. Listing checks use every in.bookmyshow.com
# slot, so these need their own small lane or alert seat reads never finish.
METADATA={'in.bookmyshow.com':(20,10)}


def carry_over(db,raw,now=None):
    """Apply source deadlines recorded by a previous deployment. Only extends waits."""
    from store import meta,setmeta
    now=time.time() if now is None else now
    schema(db);applied={}
    for item in filter(None,(part.strip() for part in (raw or '').split(','))):
        host,_,value=item.partition('=')
        until=float(value)
        if host not in POLICIES or not math.isfinite(until):raise ValueError('Invalid carried-over source deadline')
        if until<=now:continue
        db.execute('INSERT INTO source_hosts VALUES(?,?) ON CONFLICT(host) DO UPDATE SET not_before=max(not_before,excluded.not_before)',(host,until))
        if host=='services-in.bookmyshow.com':
            setmeta(db,'seat_cooldown_until',max(until,float(meta(db,'seat_cooldown_until','0'))))
        applied[host]=until
    db.commit();return applied


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS source_hosts(host TEXT PRIMARY KEY,not_before REAL DEFAULT 0)')
    db.execute("CREATE TABLE IF NOT EXISTS source_requests(id INTEGER PRIMARY KEY,at REAL,host TEXT,endpoint TEXT,status INTEGER DEFAULT 0,retry_after TEXT DEFAULT '',cf_ray TEXT DEFAULT '',outcome TEXT DEFAULT 'pending')")
    db.execute('CREATE INDEX IF NOT EXISTS source_request_host_time ON source_requests(host,at)')
    if 'manual' not in {r[1] for r in db.execute('PRAGMA table_info(source_requests)')}:
        db.execute('ALTER TABLE source_requests ADD COLUMN manual INTEGER DEFAULT 0')


def reserve(db,url,now=None,manual=False,metadata=False):
    now=time.time() if now is None else now
    parts=urlsplit(url)
    policies=POLICIES
    if parts.scheme!='https' or parts.netloc not in policies:raise ValueError('Invalid source host')
    schema(db);db.commit();db.execute('BEGIN IMMEDIATE')
    try:
        manual=bool(manual and parts.netloc in MANUAL)
        lane=1 if manual else 2 if metadata and parts.netloc in METADATA else 0
        limit,gap=(policies,MANUAL,METADATA)[lane][parts.netloc]
        row=db.execute('SELECT not_before FROM source_hosts WHERE host=?',(parts.netloc,)).fetchone()
        count,oldest=db.execute('SELECT count(*),min(at) FROM source_requests WHERE host=? AND at>? AND manual=?',(parts.netloc,now-3600,lane)).fetchone()
        # Every lane honors the host spacing, including manual and metadata reads.
        gap=max(gap,policies[parts.netloc][1])
        last=db.execute('SELECT max(at) FROM source_requests WHERE host=? AND at>?',(parts.netloc,now-3600)).fetchone()[0]
        until=max(row[0] if row else 0,(last+gap) if last is not None else 0,(oldest+3600) if count>=limit else 0)
        if until>now:raise SourceWait(until)
        cursor=db.execute('INSERT INTO source_requests(at,host,endpoint,manual) VALUES(?,?,?,?)',(now,parts.netloc,parts.path,lane))
        db.execute('DELETE FROM source_requests WHERE at<?',(now-7*86400,))
        db.commit();return cursor.lastrowid
    except Exception:db.rollback();raise


def finish(db,request_id,status,headers=None,outcome='response'):
    headers=headers or {}
    retry=str(headers.get('Retry-After',''))[:160];ray=str(headers.get('CF-Ray',''))[:160]
    db.execute('UPDATE source_requests SET status=?,retry_after=?,cf_ray=?,outcome=? WHERE id=?',(status,retry,ray,outcome,request_id))
    if status==429:
        from api_source import retry_delay
        host=db.execute('SELECT host FROM source_requests WHERE id=?',(request_id,)).fetchone()[0]
        until=time.time()+retry_delay(status,retry)
        db.execute('INSERT INTO source_hosts VALUES(?,?) ON CONFLICT(host) DO UPDATE SET not_before=max(not_before,excluded.not_before)',(host,until))
    db.commit()


def open_request(request,timeout=25,manual=False,metadata=False):
    if os.environ.get('BMS_REQUEST_BUDGET')!='1':return urllib.request.urlopen(request,timeout=timeout)
    db=connect(os.environ['BMS_DB'])
    try:
        request_id=reserve(db,request.full_url,manual=manual,metadata=metadata)
        try:
            response=urllib.request.urlopen(request,timeout=timeout)
        except urllib.error.HTTPError as error:
            finish(db,request_id,error.code,error.headers);raise
        except Exception as error:
            finish(db,request_id,0,outcome=type(error).__name__);raise
        finish(db,request_id,response.status,response.headers)
        return response
    finally:db.close()
