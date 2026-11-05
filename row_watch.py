"""Durable on-demand row loads and polling of one selected show."""
import argparse,json,os,re,signal,subprocess,sys,time
from datetime import datetime
from pathlib import Path
from store import connect,meta,setmeta,emit,record_check
import watch_preferences as prefs
from http_source import SourceWait

def schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS row_layouts(show_id TEXT PRIMARY KEY,payload TEXT,checked REAL DEFAULT 0,error TEXT DEFAULT '',due REAL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS row_alert_state(alert_id TEXT PRIMARY KEY,signature TEXT NOT NULL,available TEXT NOT NULL)")

def request(db,show_id):
    schema(db)
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(show_id,time.time())).fetchone()
    if not row:raise ValueError('Choose a future showtime from the list')
    now=time.time()
    interval,_=poll_policy(db,now,os.environ.get('BMS_PUBLIC')=='1')
    db.execute("INSERT INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?) ON CONFLICT(show_id) DO UPDATE SET due=CASE WHEN checked<? AND (due<=? OR error='') THEN excluded.due ELSE due END",(show_id,now,now-interval,now))
    db.commit()

def source_ready_at(db):
    """Earliest time the seat source may be asked again (budget, spacing or cooldown)."""
    return max(float(meta(db,key,'0')) for key in ('seat_cooldown_until','seat_next_request_at','seat_budget_until'))

def clear_source_wait(db):
    """User-requested retry: drop saved seat waits so the next queued read runs now.
    The hourly request budget still applies, and a new 429 starts a new wait."""
    schema(db);now=time.time()
    from http_source import schema as source_schema
    source_schema(db)
    for key in ('seat_cooldown_until','seat_budget_until','seat_next_request_at'):setmeta(db,key,0)
    db.execute("UPDATE source_hosts SET not_before=0 WHERE host='services-in.bookmyshow.com'")
    db.execute('UPDATE row_layouts SET due=? WHERE due>?',(now,now))
    db.commit()

def get(db,show_id):
    schema(db);row=db.execute('SELECT * FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
    if not row:return {'show_id':show_id,'checked':0,'error':'','data':{},'due':0,'next_read':0}
    result={**dict(row),'data':json.loads(row['payload'])}
    # Tell the page when a queued read can really start, so it can show the wait.
    result['next_read']=max(row['due'],source_ready_at(db)) if row['due'] else 0
    return result

def run_reader(show,timeout=75):
    command=['xvfb-run','-a',sys.executable,str(Path(__file__).with_name('row_reader.py'))]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        stdout,stderr=process.communicate(json.dumps(show),timeout=timeout)
        return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
    except subprocess.TimeoutExpired:
        # Killing xvfb-run alone leaves its browser/display children alive.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate()
        raise ValueError('Seat layout timed out. Try loading it again later.') from None

def seat_order(seat):
    """Order seat labels as people read them: 1, 2, 3, 25 rather than 1, 2, 25, 3."""
    return [(0,int(part),'') if part.isdigit() else (1,0,part) for part in re.findall(r'\d+|\D+',str(seat))]


def show_date(show):
    try:return datetime.strptime(show.get('date',''),'%Y%m%d').strftime('%d %b %Y')
    except (TypeError,ValueError):return show.get('date','')


def ingest(db,show,result):
    schema(db);old=get(db,show['id']);previous=old['data']
    known={r['id']:r for r in result['rows']};known.update({r['id']:r for r in previous.get('rows',[]) if r['id'] not in known});result['rows']=list(known.values())
    if meta(db,'notifications_paused','0')!='1':
        seen_signatures=set()
        for rule in prefs.active(db):
            p=rule['preferences']
            if p.get('row_show')!=show['id']:continue
            selected=set(p.get('rows',[]))
            signature=json.dumps([show['id'],sorted(selected)])
            state=db.execute('SELECT signature,available FROM row_alert_state WHERE alert_id=?',(rule['id'],)).fetchone()
            before={tuple(item) for item in json.loads(state['available'])} if state and state['signature']==signature else set()
            current={(s['row_id'],s['seat']) for s in result['available'] if s['row_id'] in selected}
            added=[s for s in result['available'] if s['row_id'] in selected and (s['row_id'],s['seat']) not in before]
            if not p['notify_existing'] and (not state or state['signature']!=signature):
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',(rule['id'],signature,json.dumps(sorted(current))))
                continue
            if not added:
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (rule['id'],signature,json.dumps(sorted(current))))
                continue
            if signature in seen_signatures:
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (rule['id'],signature,json.dumps(sorted(current))))
                continue
            seen_signatures.add(signature)
            now=time.time()
            pending=db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(rule['id'],)).fetchone()
            if now-rule['last_alert']>=p['interval_minutes']*60 and not pending:
                opened_rows=sorted({s['row_id'] for s in added})
                lines=['• '+r+': '+', '.join(sorted((s['seat'] for s in added if s['row_id']==r),key=seat_order)) for r in opened_rows]
                message=f"🎟 Seats now available · Row{'s' if len(opened_rows)>1 else ''} {', '.join(opened_rows)}\n\n🎬 {show['movie']}\n📍 {show['venue']}\n📅 {show_date(show)} · {show['time']}\n\n"+'\n'.join(lines)
                # Shorten the seat list, never the exact booking link at the end.
                link=f"\n\n{result['url']}";limit=950-len(link)
                if len(message)>limit:message=message[:limit-1].rstrip()+'…'
                message+=link
                try:
                    from seat_map import render
                    photo=render(show,result,opened_rows)
                except Exception:photo=None  # The text alert still carries every detail.
                emit(db,'row:'+rule['id']+':'+show['id']+':'+str(time.time_ns()),message,photo,alert_id=rule['id'])
                db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,rule['id']))
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (rule['id'],signature,json.dumps(sorted(current))))
    db.execute('UPDATE row_layouts SET payload=?,checked=?,error=?,due=0 WHERE show_id=?',(json.dumps(result),time.time(),'',show['id']));db.commit()

def schedule_active(db,now,interval=30):
    if meta(db,'notifications_paused','0')=='1':return
    for rule in prefs.active(db):
        show_id=rule['preferences'].get('row_show')
        if show_id:db.execute('UPDATE row_layouts SET due=? WHERE show_id=? AND due=0 AND checked<?',(now,show_id,now-interval))
    db.commit()

def poll_policy(db,now,cloud=True):
    until=float(meta(db,'seat_cooldown_until','0'))
    last_limit=float(meta(db,'seat_last_rate_limit','0'))
    # Recovery starts after the source wait ends, including waits longer than 15 minutes.
    recovering=bool((until and 0<=now-until<900) or
                    (last_limit and 0<=now-last_limit<900))
    return (120,60) if cloud and recovering else (60,30) if cloud else (60,0)


def next_job(db,now):
    if source_ready_at(db)>now:return None
    active=sorted({r['preferences']['row_show'] for r in prefs.active(db) if r['preferences']['row_show']})
    slots=','.join('?' for _ in active) or "''"
    # A showtime nobody has seen yet (someone is waiting on the page) goes first;
    # routine alert refreshes would otherwise take every slot of the source budget.
    # An alert showtime overdue by 4 minutes goes first, so browsing cannot starve it.
    return db.execute(f"""SELECT * FROM row_layouts WHERE due>0 AND due<=?
      ORDER BY CASE WHEN show_id IN ({slots}) AND due<? THEN 0 WHEN checked=0 AND error='' THEN 1 WHEN due<? THEN 2 WHEN show_id IN ({slots}) THEN 3 ELSE 4 END,due LIMIT 1""",(now,*active,now-240,now-90,*active)).fetchone()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);args=parser.parse_args()
    if os.environ.get('BMS_ROWS_ENABLED')!='1':return
    db=connect(args.db);schema(db);db.commit();stop=False
    def halt(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,halt);signal.signal(signal.SIGINT,halt)
    while not stop:
        try:step(db)
        except Exception as error:
            # A locked database or unexpected state must not end the worker; retry shortly.
            try:db.rollback()
            except Exception:pass
            print(f'row_watch: {type(error).__name__}; retrying in 5 seconds',flush=True)
            time.sleep(5)
    db.close()

def step(db):
    now=time.time()
    cloud = os.environ.get('BMS_PUBLIC')=='1'
    interval,spacing=poll_policy(db,now,cloud)
    schedule_active(db,now,interval)
    setmeta(db,'row_reader_heartbeat',now);db.commit()
    job=next_job(db,now)
    if not job:time.sleep(1);return
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND cutoff>?',(job['show_id'],now)).fetchone()
    if not row:
        db.execute("UPDATE row_layouts SET due=0,error='Showtime has expired' WHERE show_id=?",(job['show_id'],));db.commit();return
    show=json.loads(row[0])
    try:
        if cloud:
            # Persist request spacing so restarting cannot release a burst.
            setmeta(db,'seat_next_request_at',now+spacing)
            db.commit()
            from cloud_row_reader import read
            result=read(show)
        else:
            run=run_reader(show)
            payload=json.loads(run.stdout)
            if run.returncode or payload.get('error'):raise ValueError(payload.get('error','Seat read failed'))
            result=payload['data']
        ingest(db,show,result)
        record_check(db,'rows',show['id'],now);db.commit()
    except Exception as e:
        # Discard any partial alert state from a failed ingest before recording the failure.
        db.rollback()
        if isinstance(e,SourceWait):
            db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(e.until,job['show_id']))
            setmeta(db,'seat_budget_until',e.until);db.commit();return
        message=(str(e).partition(chr(10))[0] or type(e).__name__)[:200]
        if getattr(e,'diagnostic',None):
            setmeta(db,'seat_last_refusal',json.dumps({**e.diagnostic,'at':time.time()}))
        retry_at=time.time()+300
        if getattr(e,'status',0)==429:
            retry_at=time.time()+max(300,e.retry)
            setmeta(db,'seat_cooldown_until',retry_at)
            setmeta(db,'seat_last_rate_limit',time.time())
        record_check(db,'rows',show['id'],now,message)
        db.execute('UPDATE row_layouts SET error=?,due=? WHERE show_id=?',(message,retry_at,job['show_id']))
        db.commit()
if __name__=='__main__':main()
