"""Persistent source request budgets and safe HTTP outcome logging."""
import json,math,os,sqlite3,time,urllib.request,urllib.error
from urllib.parse import urlsplit
from store import connect

class SourceWait(RuntimeError):
    def __init__(self,until):
        self.until=until
        super().__init__('Waiting for the permitted source request budget')


LISTING_SPACING=30.0
POLICIES={'services-in.bookmyshow.com':(60,60),'in.bookmyshow.com':(120,LISTING_SPACING),'in-cdn.bmscdn.com':(6,600)}
# Seat loads someone asked for on the page: a small separate budget, so they run at once
# without taking slots from alert checks. Source cooldowns (429) still apply.
MANUAL={'services-in.bookmyshow.com':(10,30),'in.bookmyshow.com':(10,30)}
# Seat-category lookups for alert seat reads. Listing checks use every in.bookmyshow.com
# slot, so these need their own small lane or alert seat reads never finish.
METADATA={'in.bookmyshow.com':(20,30)}


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
        try:db.execute('ALTER TABLE source_requests ADD COLUMN manual INTEGER DEFAULT 0')
        except sqlite3.OperationalError as error:
            if 'duplicate column name' not in str(error):raise


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
        # Manual and metadata reads keep their gap from any read; alert reads only from other alert reads.
        last=db.execute('SELECT max(at) FROM source_requests WHERE host=? AND at>?'+('' if lane else ' AND manual=0'),(parts.netloc,now-3600)).fetchone()[0]
        until=max(row[0] if row else 0,(last+gap) if last is not None else 0,(oldest+3600) if count>=limit else 0)
        if parts.netloc=='in.bookmyshow.com':
            # All request lanes share the listing host's spacing and ceiling.
            total,first,last_all=db.execute('SELECT count(*),min(at),max(at) FROM source_requests WHERE host=? AND at>?',
                                           (parts.netloc,now-3600)).fetchone()
            until=max(until,last_all+LISTING_SPACING if last_all is not None else 0,
                      first+3600 if total>=120 else 0)
        if until>now:raise SourceWait(until)
        cursor=db.execute('INSERT INTO source_requests(at,host,endpoint,manual) VALUES(?,?,?,?)',(now,parts.netloc,parts.path,lane))
        db.execute('DELETE FROM source_requests WHERE at<?',(now-7*86400,))
        db.commit();return cursor.lastrowid
    except Exception:db.rollback();raise


def finish(db,request_id,status,headers=None,outcome='response',diagnostic=None):
    headers=headers or {}
    retry=str(headers.get('Retry-After',''))[:160];ray=str(headers.get('CF-Ray',''))[:160]
    db.execute('UPDATE source_requests SET status=?,retry_after=?,cf_ray=?,outcome=? WHERE id=?',(status,retry,ray,outcome,request_id))
    if diagnostic is not None:
        from store import setmeta
        host=db.execute('SELECT host FROM source_requests WHERE id=?',(request_id,)).fetchone()[0]
        safe_headers={key:headers.get(key) for key in ('Server','Content-Type','CF-Mitigated','Retry-After','CF-Ray','Cache-Control','Connection') if headers.get(key) is not None}
        setmeta(db,'source_observation:'+host+(':'+str(status)),json.dumps({**diagnostic,'status':status,'at':time.time(),'response_headers':safe_headers}))
    if status==429 or (status==403 and retry):
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
            finish(db,request_id,error.code,error.headers,diagnostic=getattr(error,'session_diagnostic',None));raise
        except Exception as error:
            finish(db,request_id,0,outcome=type(error).__name__);raise
        finish(db,request_id,response.status,response.headers,diagnostic=getattr(response,'session_diagnostic',None))
        return response
    finally:db.close()
