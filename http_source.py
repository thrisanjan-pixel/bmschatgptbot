"""Persistent source request budgets and safe HTTP outcome logging."""
import io,math,os,random,time,urllib.request,urllib.error
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


def env_seconds(name,default):
    try:value=float(os.environ.get(name,default))
    except ValueError:value=float(default)
    return value if math.isfinite(value) and value>=0 else float(default)


def jitter(seed):
    """Extra spacing of 0..BMS_REQUEST_JITTER seconds (default 15), fixed per previous request
    so every worker computes the same next slot, and requests never land on a fixed beat."""
    limit=env_seconds('BMS_REQUEST_JITTER',15)
    return random.Random(seed).uniform(0,limit) if limit else 0


def classify(status,headers=None):
    """Name a source outcome so blocks are not confused with ordinary errors.

    'ok', 'rate_limited' (429), 'cloudflare_block' (403 served by Cloudflare),
    'forbidden' (other 403), 'server_error' (5xx), 'network' (no HTTP reply), 'http_error'."""
    headers=headers or {}
    if status and status<400:return 'ok'
    if status==429:return 'rate_limited'
    if status==403:
        server=str(headers.get('Server','')).lower()
        return 'cloudflare_block' if 'cloudflare' in server or headers.get('CF-Ray') or headers.get('CF-Mitigated') else 'forbidden'
    if not status:return 'network'
    return 'server_error' if status>=500 else 'http_error'


def backoff(kind,failures):
    """Seconds a host rests after `failures` consecutive failures of this kind (0 = no rest).

    A 403 doubles from BMS_403_BACKOFF_BASE (default 180 s) on every repeat. Other failures
    open the circuit breaker after BMS_BREAKER_THRESHOLD (default 3) in a row, starting at
    BMS_BREAKER_COOLDOWN (default 600 s) and doubling. Both stop at BMS_BACKOFF_MAX (default 6 h)."""
    cap=env_seconds('BMS_BACKOFF_MAX',6*3600)
    if kind in ('cloudflare_block','forbidden'):
        return min(cap,env_seconds('BMS_403_BACKOFF_BASE',180)*2**min(20,failures-1))
    threshold=max(1,int(env_seconds('BMS_BREAKER_THRESHOLD',3)))
    if kind in ('network','server_error','http_error') and failures>=threshold:
        return min(cap,env_seconds('BMS_BREAKER_COOLDOWN',600)*2**min(20,failures-threshold))
    return 0


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
    # failures: consecutive failed requests; reason: why the host is resting; alerted: operator told.
    db.execute("CREATE TABLE IF NOT EXISTS source_health(host TEXT PRIMARY KEY,failures INTEGER DEFAULT 0,reason TEXT DEFAULT '',alerted INTEGER DEFAULT 0)")
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
        last,last_id=db.execute('SELECT at,id FROM source_requests WHERE host=? AND at>? ORDER BY at DESC LIMIT 1',(parts.netloc,now-3600)).fetchone() or (None,None)
        # Alert checks get random extra spacing; manual and metadata reads keep their short gap.
        spacing=gap+(jitter(last_id) if lane==0 else 0)
        until=max(row[0] if row else 0,(last+spacing) if last is not None else 0,(oldest+3600) if count>=limit else 0)
        if until>now:raise SourceWait(until)
        cursor=db.execute('INSERT INTO source_requests(at,host,endpoint,manual) VALUES(?,?,?,?)',(now,parts.netloc,parts.path,lane))
        db.execute('DELETE FROM source_requests WHERE at<?',(now-7*86400,))
        db.commit();return cursor.lastrowid
    except Exception:db.rollback();raise


def finish(db,request_id,status,headers=None,outcome='response'):
    headers=headers or {}
    retry=str(headers.get('Retry-After',''))[:160];ray=str(headers.get('CF-Ray',''))[:160]
    db.execute('UPDATE source_requests SET status=?,retry_after=?,cf_ray=?,outcome=? WHERE id=?',(status,retry,ray,outcome,request_id))
    found=db.execute('SELECT host FROM source_requests WHERE id=?',(request_id,)).fetchone()
    if found:record_outcome(db,found[0],status,headers)
    db.commit()


def record_outcome(db,host,status,headers=None,now=None):
    """Update the host's failure streak and rest time after one request.

    A 429 rests the whole host until its Retry-After (never shortened). A 403 rests it with
    exponential backoff. Repeated network or 5xx failures open the circuit breaker.
    A success closes everything. Returns the outcome kind."""
    from api_source import retry_delay
    now=time.time() if now is None else now
    headers=headers or {}
    kind=classify(status,headers)
    schema(db)
    db.execute('INSERT OR IGNORE INTO source_health(host) VALUES(?)',(host,))
    if kind=='ok':
        db.execute("UPDATE source_health SET failures=0,reason='',alerted=0 WHERE host=?",(host,));return kind
    db.execute('UPDATE source_health SET failures=failures+1,reason=? WHERE host=?',(kind,host))
    failures=db.execute('SELECT failures FROM source_health WHERE host=?',(host,)).fetchone()[0]
    rest=retry_delay(status,str(headers.get('Retry-After','')) or None) if kind=='rate_limited' else backoff(kind,failures)
    if rest:
        db.execute('INSERT INTO source_hosts(host,not_before) VALUES(?,?) ON CONFLICT(host) DO UPDATE SET not_before=max(not_before,excluded.not_before)',(host,now+rest))
        if kind=='rate_limited' and host=='services-in.bookmyshow.com':
            from store import meta,setmeta
            setmeta(db,'seat_cooldown_until',max(now+rest,float(meta(db,'seat_cooldown_until','0'))))
        alert_operator(db,host,kind,failures,now+rest)
    return kind


def alert_operator(db,host,kind,failures,until):
    """Tell the operator once per episode when a host is blocked or the breaker opens."""
    threshold=int(env_seconds('BMS_BLOCK_ALERT_AFTER',2))
    if kind!='rate_limited' and failures<threshold:return
    if db.execute('SELECT alerted FROM source_health WHERE host=?',(host,)).fetchone()[0]:return
    words={'rate_limited':'is rate-limiting us (HTTP 429)','cloudflare_block':'is blocking us at Cloudflare (HTTP 403)',
        'forbidden':'is refusing us (HTTP 403)'}.get(kind,'keeps failing, so the circuit breaker is open')
    minutes=max(1,round((until-time.time())/60))
    from store import emit
    emit(db,f'source-block:{host}:{kind}:{int(until)}',f"⚠️ BookMyShow {host} {words}. The bot pauses requests to it for about {minutes} min and resumes on its own.",alert_id="__health__")
    db.execute('UPDATE source_health SET alerted=1 WHERE host=?',(host,))


IMPERSONATE='safari'


class _Response(io.BytesIO):
    """The urllib response surface the readers use: read(), status, headers, with-block."""
    def __init__(self,url,status,headers,body):
        super().__init__(body);self.url=url;self.status=self.code=status;self.headers=headers
    def geturl(self):return self.url


def urlopen(request,timeout=25):
    """Send a urllib Request through curl_cffi so the TLS handshake matches a real browser.

    Keeps urllib's contract: returns a readable response, raises HTTPError for
    4xx/5xx (with headers, for Retry-After) and URLError when unreachable.
    """
    from curl_cffi import requests as cffi_requests
    # The impersonated browser supplies its own matching User-Agent.
    headers={k:v for k,v in request.header_items() if k.lower()!='user-agent'}
    try:
        response=cffi_requests.request(request.get_method(),request.full_url,data=request.data,headers=headers,
            timeout=timeout,impersonate=IMPERSONATE,allow_redirects=True)
    except Exception as error:
        raise urllib.error.URLError(f'{type(error).__name__}: {error}') from None
    if response.status_code>=400:
        raise urllib.error.HTTPError(request.full_url,response.status_code,response.reason or '',response.headers,io.BytesIO(response.content))
    return _Response(response.url,response.status_code,response.headers,response.content)


def open_request(request,timeout=25,manual=False,metadata=False):
    if os.environ.get('BMS_REQUEST_BUDGET')!='1':return urlopen(request,timeout=timeout)
    db=connect(os.environ['BMS_DB'])
    try:
        request_id=reserve(db,request.full_url,manual=manual,metadata=metadata)
        try:
            response=urlopen(request,timeout=timeout)
        except urllib.error.HTTPError as error:
            finish(db,request_id,error.code,error.headers);raise
        except Exception as error:
            finish(db,request_id,0,outcome=type(error).__name__);raise
        finish(db,request_id,response.status,response.headers)
        return response
    finally:db.close()
