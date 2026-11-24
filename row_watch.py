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
    prefs.photo_schema(db)
    # manual=1: someone pressed Load seat rows; it runs at once on the manual budget.
    if 'manual' not in {r[1] for r in db.execute('PRAGMA table_info(row_layouts)')}:
        db.execute('ALTER TABLE row_layouts ADD COLUMN manual INTEGER DEFAULT 0')
    # sold_out_at: when the seat source last said this show is sold out (intException 3306).
    if 'sold_out_at' not in {r[1] for r in db.execute('PRAGMA table_info(row_layouts)')}:
        db.execute('ALTER TABLE row_layouts ADD COLUMN sold_out_at REAL DEFAULT 0')

OPEN_STATUSES=('1','2','3')


def show_day(show,now=None):
    """True when the show starts (booking closes) within BMS_SHOWDAY_WINDOW seconds (default 3 h):
    held seats are most often released in the last hours before a show."""
    now=time.time() if now is None else now
    try:window=float(os.environ.get('BMS_SHOWDAY_WINDOW','10800'))
    except ValueError:window=10800
    return bool(show) and window>0 and 0<float(show.get('cutoff') or 0)-now<=window


def sold_out_recheck(show=None,now=None):
    """Seconds between seat reads of a show the listing still reports as sold out.
    Shows inside the show-day window use BMS_SHOWDAY_RECHECK (default 60 s) instead."""
    if show_day(show,now):
        from http_source import seat_gap
        return max(seat_gap(),float(os.environ.get('BMS_SHOWDAY_RECHECK','60')))
    return max(120,float(os.environ.get('BMS_SOLD_OUT_RECHECK','900')))


def listing_state(show):
    """What the showtime listing (already fetched every minute) says about seats.

    'open' when any category has seats, 'sold_out' when every category is sold out,
    None when the listing carries no category status. This costs no extra request."""
    statuses=[str(c.get('status','')) for c in (show.get('categories') or {}).values() if isinstance(c,dict)]
    if not statuses:return None
    if any(status in OPEN_STATUSES for status in statuses):return 'open'
    return 'sold_out' if all(status=='0' for status in statuses) else None


def probe_skips(db,show,job,now):
    """True when the full seat layout read can be skipped.

    Only for a show the seat source itself confirmed sold out (intException 3306): while the
    listing still says every category is sold out, the layout is read again only after the
    backstop interval. Shows with seats are never gated by the listing, which can lag."""
    confirmed=float(job['sold_out_at'] or 0)
    if not confirmed or os.environ.get('BMS_LISTING_PROBE','1')!='1' or listing_state(show)!='sold_out':return False
    return now-confirmed<sold_out_recheck(show,now)


def mark_sold_out(db,show_id,now,confirmed):
    """Store the sold-out state: no seat is open, so any later release counts as new."""
    row=db.execute('SELECT payload FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
    try:data=json.loads(row[0]) if row and row[0] else {}
    except ValueError:data={}
    data['available']=[];data['seats']=[{**seat,'available':False} for seat in data.get('seats',[])];data['sold_out']=True
    # Forget seats remembered as open for this showtime, so released seats alert again.
    db.execute("UPDATE row_alert_state SET available='[]' WHERE signature LIKE ?",(json.dumps([show_id])[:-1]+',%',))
    # A sold-out answer (from the seat source or the listing probe) is a successful check.
    db.execute('UPDATE row_layouts SET payload=?,error=?,checked=?,sold_out_at=CASE WHEN ? THEN ? ELSE sold_out_at END WHERE show_id=?',
        (json.dumps(data),'Sold out',now,int(confirmed),now,show_id))

def request(db,show_id):
    schema(db)
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(show_id,time.time())).fetchone()
    if not row:raise ValueError('Choose a future showtime from the list')
    now=time.time()
    interval,_=poll_policy(db,now,os.environ.get('BMS_PUBLIC')=='1')
    db.execute("INSERT INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?) ON CONFLICT(show_id) DO UPDATE SET due=CASE WHEN checked<? AND (due<=? OR error='') THEN excluded.due ELSE due END",(show_id,now,now-interval,now))
    db.execute('UPDATE row_layouts SET manual=1 WHERE show_id=? AND due>0 AND due<=?',(show_id,now))
    db.commit()

def source_ready_at(db):
    """Earliest time the seat source may be asked again (budget, spacing or cooldown)."""
    return max(float(meta(db,key,'0')) for key in ('seat_cooldown_until','seat_next_request_at','seat_budget_until'))

def clear_source_wait(db):
    """User-requested retry: drop the saved budget and spacing waits so the next queued read can run.
    A 429 Retry-After deadline (seat_cooldown_until, source_hosts.not_before) is never cleared:
    queued reads move up to that deadline at the earliest. The hourly request budget still applies."""
    schema(db);now=time.time()
    for key in ('seat_budget_until','seat_next_request_at'):setmeta(db,key,0)
    ready=max(now,float(meta(db,'seat_cooldown_until','0')))
    db.execute('UPDATE row_layouts SET due=? WHERE due>?',(ready,ready))
    db.commit()

def get(db,show_id):
    schema(db);row=db.execute('SELECT * FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
    if not row:return {'show_id':show_id,'checked':0,'error':'','data':{},'due':0,'next_read':0}
    result={**dict(row),'data':json.loads(row['payload'])}
    # Tell the page when a queued read can really start, so it can show the wait.
    ready=float(meta(db,'seat_cooldown_until','0')) if row['manual'] else source_ready_at(db)
    result['next_read']=max(row['due'],ready) if row['due'] else 0
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


def best_block(result,rows,size,must_include=()):
    """Most central run of `size` free seats side by side in the chosen rows, or None.

    Seats are adjacent when they sit next to each other in the layout and, when both
    are numbered, their numbers differ by one (so an aisle gap breaks the run).
    With `must_include`, only blocks holding at least one of those (row, seat) pairs count."""
    order=[r['id'] for r in result.get('rows',[]) if r['id'] in set(rows)]
    middle=(len(order)-1)/2;best=None;wanted=set(must_include)
    for rank,row_id in enumerate(order):
        seats=[s for s in result.get('seats',[]) if s['row_id']==row_id]
        run=[]
        for index,seat in enumerate(seats):
            if not seat.get('available'):run=[];continue
            a,b=str(run[-1][1]['seat']) if run else '',str(seat['seat'])
            if run and a.isdigit() and b.isdigit() and abs(int(a)-int(b))!=1:run=[]
            run.append((index,seat))
            if len(run)<size:continue
            window=run[-size:]
            if wanted and not any((row_id,s['seat']) in wanted for _,s in window):continue
            centre=abs(sum(i for i,_ in window)/size-(len(seats)-1)/2)/max(1,len(seats))
            score=(round(centre,3),abs(rank-middle))
            if best is None or score<best[0]:best=(score,row_id,[s['seat'] for _,s in window])
    return {'row_id':best[1],'seats':best[2]} if best else None


def seat_span(seats):
    first,last=str(seats[0]),str(seats[-1])
    return f'{first}–{last}' if first.isdigit() and last.isdigit() and len(seats)>2 else ', '.join(map(str,seats))


def show_date(show):
    try:return datetime.strptime(show.get('date',''),'%Y%m%d').strftime('%d %b %Y')
    except (TypeError,ValueError):return show.get('date','')


def count_line(counts):
    """'3D GOLD · 14 seats open · 3D GOLD. · 6 seats open', or the sentence for no open seat."""
    if not sum(counts.values()):return 'No seats open in '+', '.join(counts)+' now'
    return ' · '.join(f"{name} · {n} seat{'s' if n!=1 else ''} open" for name,n in counts.items())

def process_seat_photos(db,show,result):
    """After a seat read: send the seat-map follow-ups and the seat-count gated alerts waiting on this showtime."""
    now=time.time();prefs.prune_seat_photos(db,now)
    for r in db.execute('SELECT * FROM pending_seat_photos WHERE show_id=?',(show['id'],)).fetchall():
        names=json.loads(r['categories']);category=lambda row_id:prefs.normalized(row_id.rsplit(' / ',1)[0])
        # "3D GOLD." is another category than "3D GOLD", so names match whole, not by prefix.
        counts={n:sum(1 for x in result['available'] if category(x['row_id'])==prefs.normalized(n)) for n in names}
        line=count_line(counts)
        if r['mode']=='gate':
            row=db.execute('SELECT payload FROM alerts WHERE id=?',(r['alert_id'],)).fetchone()
            if not row:db.execute('DELETE FROM pending_seat_photos WHERE alert_id=? AND show_id=?',(r['alert_id'],r['show_id']));continue
            p=json.loads(row[0])
            if sum(counts.values())<p.get('min_seats',0):
                db.execute('UPDATE pending_seat_photos SET next_read=? WHERE alert_id=? AND show_id=?',(now+max(120,p.get('interval_minutes',10)*60),r['alert_id'],r['show_id']));continue
            text=r['text'].rstrip()+'\n\n'+prefs.PHOTO_MARK+' '+line
        else:
            try:
                from api_source import seat_layout_url
                link=seat_layout_url(show)
            except Exception:link=result.get('url','')
            text=f"{prefs.PHOTO_MARK} {line}\n\n🎬 {show['movie']}\n📍 {show['venue']}\n📅 {show_date(show)} · {show['time']}"+(f'\n\n{link}' if link else '')
        wanted={prefs.normalized(n) for n in names}
        try:
            from seat_map import render
            photo=render(show,result,[x['id'] for x in result['rows'] if category(x['id']) in wanted])
        except Exception:photo=None  # The text still carries the counts.
        emit(db,r['mode']+':'+r['alert_id']+':'+r['show_id']+':'+str(time.time_ns()),text,photo,alert_id=r['alert_id'])
        if r['mode']=='gate':db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,r['alert_id']))
        db.execute('DELETE FROM pending_seat_photos WHERE alert_id=? AND show_id=?',(r['alert_id'],r['show_id']))

def ingest(db,show,result):
    schema(db);old=get(db,show['id']);previous=old['data']
    # Seats open now that were taken at the last good read; the first read marks none.
    if previous.get('seats'):
        was_open={(s['row_id'],s['seat']) for s in previous.get('available',[])}
        result['new']=[s for s in result['available'] if (s['row_id'],s['seat']) not in was_open]
    else:result['new']=[]
    known={r['id']:r for r in result['rows']};known.update({r['id']:r for r in previous.get('rows',[]) if r['id'] not in known});result['rows']=list(known.values())
    if meta(db,'notifications_paused','0')!='1':
        seen_signatures=set();shows=None
        for rule in prefs.active(db):
            p=rule['preferences']
            if p.get('row_show')==prefs.ANY_SHOW and shows is None:shows=prefs.future_shows(db)
            if show['id'] not in prefs.row_targets(db,p,shows):continue
            # A '*' alert keeps its seat memory per showtime, so seats open at one never hide another's.
            key=rule['id']+':'+show['id'] if p['row_show']==prefs.ANY_SHOW else rule['id']
            selected=set(p.get('rows',[]))
            signature=json.dumps([show['id'],sorted(selected)]+([p['party_size']] if p.get('party_size') else []))
            state=db.execute('SELECT signature,available FROM row_alert_state WHERE alert_id=?',(key,)).fetchone()
            before={tuple(item) for item in json.loads(state['available'])} if state and state['signature']==signature else set()
            current={(s['row_id'],s['seat']) for s in result['available'] if s['row_id'] in selected}
            added=[s for s in result['available'] if s['row_id'] in selected and (s['row_id'],s['seat']) not in before]
            if not p['notify_existing'] and (not state or state['signature']!=signature):
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',(key,signature,json.dumps(sorted(current))))
                continue
            if not added:
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (key,signature,json.dumps(sorted(current))))
                continue
            minimum=p.get('min_seats',0)
            if minimum:
                if len(current)<minimum:
                    # Remember what is open so the seat that reaches the minimum counts as new.
                    db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',(key,signature,json.dumps(sorted(current))))
                    continue
                # The count has just reached the minimum: name every open seat, not only the newest.
                if len(before)<minimum:added=[s for s in result['available'] if s['row_id'] in selected]
            party=p.get('party_size',0)
            block=best_block(result,selected,party,{(s['row_id'],s['seat']) for s in added}) if party else None
            if party and not block:
                # Not enough seats side by side yet; remember what is open so the seat that
                # completes a block still counts as new.
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (key,signature,json.dumps(sorted(current))))
                continue
            if signature in seen_signatures:
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (key,signature,json.dumps(sorted(current))))
                continue
            seen_signatures.add(signature)
            now=time.time()
            pending=db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(rule['id'],)).fetchone()
            # The first message for a new row selection is never held back by the repeat gap of earlier messages.
            fresh=not state or state['signature']!=signature
            if (fresh or now-rule['last_alert']>=p['interval_minutes']*60) and not pending:
                opened_rows=sorted({s['row_id'] for s in added})
                lines=[]
                for r in opened_rows:
                    seats=sorted((s['seat'] for s in added if s['row_id']==r),key=seat_order)
                    price=prefs.price_text(show,r.rsplit(' / ',1)[0])
                    lines.append('• '+r+': '+', '.join(seats)+f"  ({len(seats)} seat{'s' if len(seats)>1 else ''}{' · '+price+' each' if price else ''})")
                message=f"🎟 {len(added)} seat{'s' if len(added)>1 else ''} now available · Row{'s' if len(opened_rows)>1 else ''} {', '.join(opened_rows)}\n\n🎬 {show['movie']}\n📍 {show['venue']}\n📅 {show_date(show)} · {show['time']}\n\n"
                if block:message+=f"⭐ Best {party} together: {block['row_id']} · seats {seat_span(block['seats'])}\n\n"
                message+='\n'.join(lines)
                # Shorten the seat list, never the exact booking link at the end.
                link=f"\n\n{result['url']}";limit=950-len(link)
                if len(message)>limit:message=message[:limit-1].rstrip()+'…'
                message+=link
                try:
                    from seat_map import render
                    photo=render(show,result,opened_rows,{(block['row_id'],s) for s in block['seats']} if block else ())
                except Exception:photo=None  # The text alert still carries every detail.
                emit(db,'row:'+rule['id']+':'+show['id']+':'+str(time.time_ns()),message,photo,alert_id=rule['id'])
                db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,rule['id']))
                db.execute('INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)',
                    (key,signature,json.dumps(sorted(current))))
    process_seat_photos(db,show,result)
    from alert_health import rows as mark_rows
    mark_rows(db,show)
    db.execute('UPDATE row_layouts SET payload=?,checked=?,error=?,due=0,manual=0,sold_out_at=0 WHERE show_id=?',(json.dumps(result),time.time(),'',show['id']));db.commit()

def targets(db,now=None):
    """(sorted showtime ids read for active row alerts or waiting seat photos, the subset an urgent alert reads)."""
    active=set(prefs.seat_photo_shows(db,now));urgent=set();shows=None
    for rule in prefs.active(db):
        p=rule['preferences']
        if not p['row_show']:continue
        if p['row_show']==prefs.ANY_SHOW and shows is None:shows=prefs.future_shows(db)
        found=prefs.row_targets(db,p,shows);active.update(found)
        if p.get('urgent'):urgent.update(found)
    return sorted(active),urgent

def schedule_active(db,now,interval=30):
    schema(db);prefs.prune_seat_photos(db,now)
    # A seat photo is read even when every alert is paused or finished.
    for show_id in prefs.seat_photo_shows(db,now):
        db.execute("INSERT OR IGNORE INTO row_layouts(show_id,payload) VALUES(?,'{}')",(show_id,))
        db.execute('UPDATE row_layouts SET due=? WHERE show_id=? AND due=0',(now,show_id))
    if meta(db,'notifications_paused','0')=='1':db.commit();return
    wanted=targets(db,now)[0]
    for show_id in wanted:
        # A '*' alert finds new showtimes on its own, so their rows may not exist yet.
        db.execute("INSERT OR IGNORE INTO row_layouts(show_id,payload) VALUES(?,'{}')",(show_id,))
        db.execute('UPDATE row_layouts SET due=? WHERE show_id=? AND due=0 AND checked<?',(now,show_id,now-interval))
    # A showtime an alert stopped watching (paused, edited or finished) drops its queued seat read.
    # Only showtimes queued for an alert are dropped; a "Load seat rows" press is never in this list.
    try:before=set(json.loads(meta(db,'seat_alert_targets','[]')))
    except ValueError:before=set()
    for show_id in before-set(wanted):
        db.execute('UPDATE row_layouts SET due=0 WHERE show_id=? AND due>0 AND manual=0',(show_id,))
    setmeta(db,'seat_alert_targets',json.dumps(wanted))
    # A sold-out show waits for its backstop re-check, unless the listing shows seats again.
    for row in db.execute("SELECT r.show_id,s.payload FROM row_layouts r JOIN sessions s ON s.id=r.show_id WHERE r.error='Sold out' AND r.due>?",(now,)).fetchall():
        try:show=json.loads(row['payload'])
        except ValueError:continue
        if listing_state(show)=='open':db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now,row['show_id']))
    db.commit()

def poll_policy(db,now,cloud=True):
    until=float(meta(db,'seat_cooldown_until','0'))
    last_limit=float(meta(db,'seat_last_rate_limit','0'))
    # Recovery starts after the source wait ends, including waits longer than 15 minutes.
    recovering=bool((until and 0<=now-until<900) or
                    (last_limit and 0<=now-last_limit<900))
    from http_source import SEAT_HOST,policy
    gap=policy(SEAT_HOST,now)[1]
    return (max(120,2*gap),gap) if cloud and recovering else (gap,gap) if cloud else (60,0)


def queue_order(db,now):
    """ORDER BY clause and its parameters for the seat-read queue."""
    active,urgent=targets(db,now);rush=prefs.seat_photo_shows(db,now)
    # Showtimes inside the show-day window jump the queue like an urgent alert's.
    try:window=float(os.environ.get('BMS_SHOWDAY_WINDOW','10800'))
    except ValueError:window=10800
    if window>0 and active:
        clock=time.time() if now is None else now
        urgent|={r[0] for r in db.execute('SELECT id FROM sessions WHERE cutoff>? AND cutoff<=?',(clock,clock+window)) if r[0] in set(active)}
    urgent=sorted(urgent)
    slots=','.join('?' for _ in active) or "''";hot=','.join('?' for _ in urgent) or "''";first=','.join('?' for _ in rush) or "''"
    # An alert showtime overdue by 2 minutes goes first, so browsing cannot starve it.
    # Then a showtime nobody has seen yet (someone is waiting on the page);
    # routine alert refreshes would otherwise take every slot of the source budget.
    # An urgent alert's showtimes count their overdue time three times (2 minutes becomes 40 s).
    # A waiting seat photo ranks first as soon as it is due.
    order=f"CASE WHEN show_id IN ({first}) THEN 0 WHEN show_id IN ({slots}) AND due<(CASE WHEN show_id IN ({hot}) THEN ? ELSE ? END) THEN 0 WHEN checked=0 AND error='' THEN 1 WHEN due<? THEN 2 WHEN show_id IN ({slots}) THEN 3 ELSE 4 END,CASE WHEN show_id IN ({hot}) THEN due-(?-due)*2 ELSE due END"
    return order,(*rush,*active,*urgent,now-40,now-120,now-90,*active,*urgent,now),set(active)

def next_job(db,now):
    if source_ready_at(db)>now:return None
    order,params,_=queue_order(db,now)
    return db.execute(f'SELECT * FROM row_layouts WHERE due>0 AND due<=? ORDER BY {order} LIMIT 1',(now,*params)).fetchone()

def queue(db,now=None,spacing=60):
    """Queued seat reads in run order, each with an estimated start time."""
    schema(db);now=time.time() if now is None else now
    order,params,active=queue_order(db,now)
    rows=db.execute(f"""SELECT r.show_id,r.due,r.checked,s.payload FROM row_layouts r LEFT JOIN sessions s ON s.id=r.show_id
      WHERE r.due>0 ORDER BY CASE WHEN r.manual=1 AND r.due<=? THEN 0 WHEN r.due<=? THEN 1 ELSE 2 END,{order}""",(now,now,*params)).fetchall()
    slot=max(now,source_ready_at(db));items=[];instant=manual_job(db,now)
    for row in rows:
        show=json.loads(row['payload'] or '{}')
        if instant and row['show_id']==instant['show_id']:start=now
        else:start=max(slot,row['due']);slot=start+spacing
        items.append({'show_id':row['show_id'],'movie':show.get('movie',''),'venue':show.get('venue',''),
            'date':show_date(show),'time':show.get('time',''),'alert':row['show_id'] in active,
            'refresh':bool(row['checked']),'eta':start})
    return items

def manual_job(db,now):
    """A Load seat rows request that may run now, outside the alert queue."""
    if float(meta(db,'seat_cooldown_until','0'))>now:return None
    return db.execute('SELECT * FROM row_layouts WHERE manual=1 AND due>0 AND due<=? ORDER BY due LIMIT 1',(now,)).fetchone()

def dequeue(db,show_id):
    """Drop a queued seat read. Alert showtimes keep polling until their alert is paused."""
    schema(db)
    if show_id in queue_order(db,time.time())[2]:raise ValueError('This showtime belongs to an active alert. Pause the alert to stop its seat checks.')
    db.execute('UPDATE row_layouts SET due=0,manual=0 WHERE show_id=?',(show_id,));db.commit()

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

def worker_mode():
    """'worker' when seat layouts are read by the home seat worker (BMS_SEAT_SOURCE=worker)."""
    return os.environ.get('BMS_SEAT_SOURCE','server')=='worker'


def worker_online(db,now):
    stale=max(30,float(os.environ.get('BMS_WORKER_STALE','180')))
    return now-float(meta(db,'seat_worker_seen','0'))<stale


def note_worker_state(db,now):
    """Record whether the home seat worker is reachable; tell the operator once when it drops."""
    online=worker_online(db,now)
    was=meta(db,'seat_worker_offline','0')=='1'
    if online and was:setmeta(db,'seat_worker_offline',0)
    elif not online and not was:
        setmeta(db,'seat_worker_offline',1)
        fallback=os.environ.get('BMS_WORKER_FALLBACK','none')=='server'
        emit(db,f'seat-worker-offline:{int(now)}','⚠️ The home seat worker is offline. '+
            ('The server reads seat layouts itself until it is back.' if fallback else
             'Seat-row alerts pause until it is back; showtime alerts keep working.'),alert_id='__health__')
    return online


def take_job(db,now,cloud=True):
    """Next seat read to run: (job row, manual flag, show) or None. Expired showtimes are closed here."""
    job=manual_job(db,now) if cloud else None;manual=bool(job)
    job=job or next_job(db,now)
    if not job:return None
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND cutoff>?',(job['show_id'],now)).fetchone()
    if not row:
        db.execute("UPDATE row_layouts SET due=0,manual=0,error='Showtime has expired' WHERE show_id=?",(job['show_id'],));db.commit();return None
    show=json.loads(row[0])
    if not manual and job['show_id'] in queue_order(db,now)[2] and probe_skips(db,show,job,now):
        # The listing says sold out and the last layout read is recent: spend no seat request.
        mark_sold_out(db,job['show_id'],now,False)
        db.execute('UPDATE row_layouts SET due=?,manual=0 WHERE show_id=?',(now+sold_out_recheck(show,now),job['show_id']))
        record_check(db,'rows',show['id'],now);db.commit();return None
    return job,manual,show


def record_failure(db,show_id,show,e,started,manual=False):
    """Store a failed seat read (or a sold-out answer) and schedule the retry."""
    if isinstance(e,SourceWait):
        # A short manual wait (10 s gap) retries soon; a used-up manual budget joins the normal queue.
        if manual:
            if e.until-time.time()<=60:db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(e.until,show_id))
            else:db.execute('UPDATE row_layouts SET manual=0 WHERE show_id=?',(show_id,))
            db.commit();return
        # Leave the job's due alone so it keeps its place in the queue; only the budget waits.
        setmeta(db,'seat_budget_until',e.until);db.commit();return
    if getattr(e,'sold_out',False):
        # Sold out is a normal answer, not a source error. Re-check on the slow backstop;
        # the listing probe brings the read forward as soon as seats reappear.
        mark_sold_out(db,show_id,time.time(),True)
        retry_at=time.time()+sold_out_recheck(show) if show_id in queue_order(db,time.time())[2] else 0
        db.execute('UPDATE row_layouts SET due=?,manual=0 WHERE show_id=?',(retry_at,show_id))
        record_check(db,'rows',show['id'],started);db.commit();return
    message=(str(e).partition(chr(10))[0] or type(e).__name__)[:200]
    if getattr(e,'diagnostic',None):
        setmeta(db,'seat_last_refusal',json.dumps({**e.diagnostic,'at':time.time()}))
    retry_at=time.time()+max(300,getattr(e,'retry',0) or 0)
    if getattr(e,'status',0)==429:
        retry_at=time.time()+max(300,e.retry)
        setmeta(db,'seat_cooldown_until',retry_at)
        setmeta(db,'seat_last_rate_limit',time.time())
    record_check(db,'rows',show['id'],started,message)
    # A show no alert watches (only "Load seat rows" was pressed) is not retried: the page shows the error.
    if show_id not in queue_order(db,time.time())[2]:retry_at=0
    db.execute('UPDATE row_layouts SET error=?,due=?,manual=0 WHERE show_id=?',(message,retry_at,show_id))
    db.commit()


WORKER_LEASE=120  # Seconds a handed-out seat read stays reserved for the home worker.


def claim(db,now=None):
    """Hand the home seat worker its next seat read, or None. Leases it so it is not handed out twice."""
    schema(db);now=time.time() if now is None else now
    setmeta(db,'seat_worker_seen',now);db.commit()
    taken=take_job(db,now)
    if not taken:return None
    job,manual,show=taken
    db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now+WORKER_LEASE,job['show_id']))
    setmeta(db,'seat_worker_lease:'+job['show_id'],json.dumps({'at':now,'manual':manual}));db.commit()
    return {'show_id':job['show_id'],'manual':manual,'show':show}


class WorkerFailure(ValueError):
    """A failure the home worker reported, carrying the same fields as a local one."""
    def __init__(self,error):
        super().__init__(str(error.get('message') or 'Seat read failed')[:200])
        self.status=int(error.get('status') or 0)
        self.retry=max(0,float(error.get('retry') or 0))
        self.sold_out=bool(error.get('sold_out'))


def submit(db,show_id,data=None,error=None,now=None):
    """Store what the home seat worker read for a leased showtime."""
    schema(db);now=time.time() if now is None else now
    setmeta(db,'seat_worker_seen',now)
    lease=meta(db,'seat_worker_lease:'+str(show_id),'')
    if not lease:raise ValueError('This seat read was not handed out')
    lease=json.loads(lease);db.execute('DELETE FROM meta WHERE key=?',('seat_worker_lease:'+show_id,))
    row=db.execute('SELECT payload FROM sessions WHERE id=?',(show_id,)).fetchone()
    if not row:db.commit();raise ValueError('Unknown showtime')
    show=json.loads(row[0])
    if error is not None:
        if not isinstance(error,dict):raise ValueError('Invalid error')
        if error.get('wait_until'):
            # The worker's own budget said wait: put the read back at that time, keeping its place.
            db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(min(float(error['wait_until']),now+3600),show_id));db.commit();return
        record_failure(db,show_id,show,WorkerFailure(error),lease['at'],lease.get('manual',False));return
    if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ('rows','available')):
        raise ValueError('Invalid seat layout')
    try:
        ingest(db,show,data);record_check(db,'rows',show_id,lease['at']);db.commit()
    except Exception as e:
        db.rollback();record_failure(db,show_id,show,e,lease['at'],lease.get('manual',False))


def release_expired_leases(db,now):
    """A worker that died mid-read leaves a lease; after it lapses the read is due again by itself."""
    for key, value in db.execute("SELECT key,value FROM meta WHERE key LIKE 'seat_worker_lease:%'").fetchall():
        try:at=json.loads(value)['at']
        except (ValueError,KeyError,TypeError):at=0
        if now-at>WORKER_LEASE:db.execute('DELETE FROM meta WHERE key=?',(key,))


def step(db):
    now=time.time()
    cloud = os.environ.get('BMS_PUBLIC')=='1'
    interval,spacing=poll_policy(db,now,cloud)
    schedule_active(db,now,interval)
    setmeta(db,'row_reader_heartbeat',now);db.commit()
    if worker_mode():
        release_expired_leases(db,now)
        online=note_worker_state(db,now);db.commit()
        # The home worker claims reads through the dashboard. Without it, the server reads
        # only when BMS_WORKER_FALLBACK=server; otherwise seat reads wait for the worker.
        if online or os.environ.get('BMS_WORKER_FALLBACK','none')!='server':time.sleep(1);return
    taken=take_job(db,now,cloud)
    if not taken:time.sleep(1);return
    job,manual,show=taken
    try:
        if cloud:
            # Persist request spacing so restarting cannot release a burst.
            if not manual:setmeta(db,'seat_next_request_at',now+spacing)
            db.commit()
            from cloud_row_reader import read
            result=read(show,manual=True) if manual else read(show)
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
        record_failure(db,job['show_id'],show,e,now,manual)
if __name__=='__main__':main()
