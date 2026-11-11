"""Saved personal watch filters and conservative Telegram pacing."""
import datetime as dt
import json
import os
import re
import sqlite3
import time
import uuid
from store import emit,meta,setmeta

ANY_SHOW='*'  # row_show value meaning every showtime that matches the alert's filters
ROW_ANY_LIMIT=20
FIELDS=('movies','cinemas','screens','languages','formats','dates','categories')
VENUE_SCREENS={
    'PRHN':['PCX SCREEN','PCX HDR BY BARCO','BARCO FLAGSHIP 4K LASER','BARCO 4K LASER',
            'BARCO 4K SMART LASER','PCX Infinity Vis 3D','Infinity Vision 3D','Infinity Vision 2D'],
    'AMBH':['BARCO FLAGSHIP LASER DOLBY ATMOS','M B LUXE','VIP SCREEN','HDR BY BARCO'],
    'ALUC':['DOLBY CINEMA'],
}

def normalized(value):return ' '.join(str(value).casefold().split())
def price_text(show,name):
    """Listed ticket price for a seat type, e.g. '₹350', or '' when BookMyShow gives none."""
    for category in show.get('categories',{}).values():
        if normalized(category.get('name',''))==normalized(name):
            try:value=float(category.get('price') or 0)
            except (TypeError,ValueError):return ''
            return f'₹{value:g}' if value>0 else ''
    return ''
def with_price(show,name):
    price=price_text(show,name);return f'{name} ({price})' if price else str(name)
def category_matches(name,p):return not p['categories'] or normalized(name) in {normalized(v) for v in p['categories']}
def format_matches(actual,wanted):
    actual=normalized(actual);wanted=normalized(wanted)
    if wanted in ('2d','3d'):return bool(re.search(r'(?<![a-z0-9])'+wanted+r'(?![a-z0-9])',actual))
    if wanted in ('imax','4dx'):return wanted in actual
    return actual==wanted
def defaults():
    return {**{k:[] for k in FIELDS},'time_from':'','time_to':'','new_shows':True,'availability':True,'enabled':False,'once':True,'notify_existing':True,'interval_minutes':10,'row_show':'','rows':[],'party_size':0,'request_state':'','urgent':False,'open_on_pc':False,'open_on_pc_until':0,'min_seats':0}

def future_shows(db):
    """Active future showtimes as (cutoff, show), soonest first."""
    return [(r[1],json.loads(r[0])) for r in db.execute('SELECT payload,cutoff FROM sessions WHERE active=1 AND cutoff>? ORDER BY cutoff',(time.time(),))]

def row_targets(db,p,shows=None):
    """Showtime ids a row alert reads: its one showtime, or for '*' the soonest matching ones."""
    if not p.get('row_show'):return []
    if p['row_show']!=ANY_SHOW:return [p['row_show']]
    probe={**p,'categories':[]};found=[]
    for _,show in (future_shows(db) if shows is None else shows):
        try:
            if matches(show,probe):found.append(show['id'])
        except (KeyError,TypeError,AttributeError):continue  # one odd stored show must not break every alert
        if len(found)>=ROW_ANY_LIMIT:break
    return found

def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alerts(
        id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL,
        updated REAL NOT NULL,last_alert REAL NOT NULL DEFAULT 0,
        completed REAL NOT NULL DEFAULT 0)''')
    if db.execute('SELECT 1 FROM alerts LIMIT 1').fetchone():return
    if meta(db,'alerts_migration_complete','0')=='1':return
    try:old={**defaults(),**json.loads(meta(db,'watch_preferences','{}'))}
    except (ValueError,TypeError):old=defaults()
    if old['enabled'] or old['row_show'] or any(old[key] for key in FIELDS):
        now=time.time()
        db.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?)',('legacy',json.dumps(old),now,now,
            float(meta(db,'watch_last_alert','0')),float(meta(db,'watch_completed','0'))))
        db.execute("UPDATE outbox SET alert_id='legacy' WHERE alert_id='' AND delivered=0")
        db.commit()

def rules(db):
    ensure(db);expire_arms(db)
    result=[];parsed=[];shows=None
    for row in db.execute('SELECT * FROM alerts ORDER BY created DESC').fetchall():
        p={**defaults(),**json.loads(row['payload'])}
        if p['row_show']==ANY_SHOW and shows is None:shows=future_shows(db)
        parsed.append((row,p,row_targets(db,p,shows)))
    # Seat reads share one budget, so the quiet period grows with every watched showtime, not every alert.
    row_count=sum(len(t) for _,p,t in parsed if p['enabled'] and p['row_show'])
    for row,p,targets in parsed:
        rule={**dict(row),'preferences':p,'row_targets':len(targets),'open_on_pc_until':p['open_on_pc_until'] if p['open_on_pc'] else 0};state=p.get('request_state','')
        expired=False
        if p['dates']:
            from api_source import end_day
            expired=max(end_day(day) for day in p['dates'])<=time.time()
        if p['row_show'] and p['row_show']!=ANY_SHOW:
            show=db.execute('SELECT cutoff FROM sessions WHERE id=?',(p['row_show'],)).fetchone()
            expired=expired or bool(show and show[0]<=time.time())
        rule['status']='cancelled' if state=='cancelled' else 'completed' if rule['completed'] or state=='booked' else 'missed' if expired and p['enabled'] else 'active' if p['enabled'] else 'paused'
        message=db.execute('SELECT text FROM outbox WHERE alert_id=? ORDER BY created DESC LIMIT 1',(rule['id'],)).fetchone()
        link=re.search(r'https://in\.bookmyshow\.com/movies/hyderabad/seat-layout/[A-Za-z0-9/]+',message[0]) if message else None
        rule['booking_url']=link[0] if link else ''
        checked=0;error='';due=0;waiting_for_bookings=False
        if p['row_show'] and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='row_layouts'").fetchone():
            layouts=db.execute(f"SELECT show_id,checked,error,due FROM row_layouts WHERE show_id IN ({','.join('?'*len(targets))})",targets).fetchall() if targets else []
            read=[l['checked'] for l in layouts if l['checked']>0]
            # The oldest read is the health signal: one fresh showtime must not hide stale ones.
            checked=min(read) if read else 0
            due=min((l['due'] for l in layouts if l['due']>0),default=0)
            if len(layouts)==1:error=layouts[0]['error']
            elif layouts:
                latest=db.execute(f"SELECT job_key FROM check_stats WHERE source='rows' AND job_key IN ({','.join('?'*len(targets))}) ORDER BY last_attempt DESC LIMIT 1",targets).fetchone()
                error=next((l['error'] for l in layouts if latest and l['show_id']==latest[0]),'')
        elif not p['row_show']:
            jobs=[r for r in db.execute('SELECT id,payload,checked,error,due FROM jobs') if normalized(json.loads(r['payload']).get('movie','')) in {normalized(m) for m in p['movies']}]
            checked=max((r['checked'] for r in jobs),default=0)
            # Report the latest listing check, not an old failure on any of the movie's listings.
            ids=[r['id'] for r in jobs];latest=None
            if ids and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='check_stats'").fetchone():
                latest=db.execute(f"SELECT error FROM check_stats WHERE source='listings' AND job_key IN ({','.join('?'*len(ids))}) ORDER BY last_attempt DESC LIMIT 1",ids).fetchone()
            error=latest[0] if latest else next((r['error'] for r in jobs if r['error']),'')
            due=min((r['due'] for r in jobs),default=0)
            waiting_for_bookings = bool(jobs) and all(
                meta(db, 'waiting_show:'+r['id']) == '1' for r in jobs
            )
        now=time.time()
        cooldown=float(meta(db,'seat_cooldown_until' if p['row_show'] else 'source_cooldown','0'))
        budget=float(meta(db,'seat_budget_until' if p['row_show'] else 'listing_budget_until','0'))
        if max(cooldown,budget)>now:due=max(due,cooldown,budget)
        delivery=db.execute('SELECT count(*),coalesce(max(attempts),0) FROM outbox WHERE alert_id=? AND delivered=0',(rule['id'],)).fetchone()
        # Routine 1-minute spacing is normal; only a long budget wait is worth showing.
        health='Source unavailable' if error else 'Waiting for request budget' if budget>now+300 else 'Checks delayed' if checked and cooldown<=now and time.time()-checked>(max(180,240*row_count) if p['row_show'] else 180) else 'Watching seats' if p['row_show'] else 'Waiting for bookings' if waiting_for_bookings else 'Watching availability' if checked else 'Waiting for listings'
        rule['health']={'label':health if rule['status']=='active' else rule['status'].capitalize(),'last_success':checked,'error':error,'next_check':due,'retry_until':max(cooldown,budget) if max(cooldown,budget)>now else 0,'pending_messages':delivery[0],'delivery_attempts':delivery[1]}
        keys=targets if p['row_show'] else [r['id'] for r in db.execute('SELECT id,payload FROM jobs') if normalized(json.loads(r['payload']).get('movie','')) in {normalized(m) for m in p['movies']}]
        source='rows' if p['row_show'] else 'listings'
        intervals=[]
        for key in keys:
            intervals.extend(r[0] for r in db.execute('SELECT gap FROM check_intervals WHERE source=? AND job_key=? AND at>=?',(source,key,max(rule['created'],time.time()-7*86400))))
        delays=db.execute('SELECT avg(delivered-created),max(delivered-created),count(*) FROM outbox WHERE alert_id=? AND delivered>0',(rule['id'],)).fetchone()
        ongoing_gap=max(0,now-max(checked,rule['created'])) if rule['status']=='active' else None
        rule['report']={'ongoing_gap':ongoing_gap,'longest_gap_including_current':max([*intervals,ongoing_gap] if ongoing_gap is not None else intervals,default=None),'window_days':7,'gap_samples':len(intervals),'average_gap':sum(intervals)/len(intervals) if intervals else None,'longest_gap':max(intervals) if intervals else None,'delivery_average':delays[0],'delivery_longest':delays[1],'deliveries':delays[2],'last_success':checked}
        result.append(rule)
    return result

def active(db):
    return [rule for rule in rules(db) if rule['status']=='active']


def prime_availability(db):
    """Notify once when a new category alert already matches open seats."""
    from api_source import seat_layout_url

    db.execute('''CREATE TABLE IF NOT EXISTS alert_initial_state(
        alert_id TEXT PRIMARY KEY,signature TEXT NOT NULL)''')
    seen=set()
    pending=[]
    for rule in active(db):
        p=rule['preferences']
        if not p['availability'] or p['row_show']:continue
        signature=json.dumps({key:p[key] for key in (*FIELDS,'time_from','time_to','availability','notify_existing')},sort_keys=True)
        state=db.execute('SELECT signature FROM alert_initial_state WHERE alert_id=?',(rule['id'],)).fetchone()
        if state and state[0]==signature:
            seen.add(signature)
            continue
        pending.append((rule,signature))
    if not pending:return
    shows=[json.loads(row[0]) for row in db.execute(
        'SELECT payload FROM sessions WHERE active=1 AND cutoff>?',(time.time(),))]
    for rule,signature in pending:
        p=rule['preferences']
        if signature not in seen and p['notify_existing']:
            found=[]
            for show in shows:
                if not matches(show,p):continue
                opened=sorted({str(category['name']) for category in show['categories'].values()
                    if category['status'] in ('1','2','3') and
                    category_matches(category['name'],p)})
                # A stored show without a valid seat-layout identity cannot carry an exact link.
                try:link=seat_layout_url(show)
                except Exception:continue
                if opened:found.append((show,opened,link))
            # Nothing to say yet, but still record state so it is not rescanned every second.
            if found and not db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(rule['id'],)).fetchone():
                lines=[]
                for show,opened,link in found[:3]:
                    lines.append(' · '.join(str(show.get(key) or '') for key in ('venue','date','time'))+'\n'+
                        ', '.join(with_price(show,name) for name in opened)+'\n'+link)
                extra=f'\n\n{len(found)-3} more matching showtimes are open.' if len(found)>3 else ''
                head='🎬 '+str(found[0][0].get('movie') or '')+'\nSeats already available for this alert:\n\n'
                message=head+'\n\n'.join(lines)+extra
                if p['min_seats'] and rows_enabled():
                    # Hold each showtime's alert until a seat-map read shows enough open seats.
                    for (show,opened,link),line in zip(found[:3],lines):queue_seat_photo(db,rule['id'],show['id'],opened,'gate',head+line)
                else:
                    emit(db,'initial:'+rule['id']+':'+signature,(message+('\n\n(seat count unavailable)' if p['min_seats'] else ''))[:3900],alert_id=rule['id'])
                    if rows_enabled():
                        for show,opened,link in found[:3]:queue_seat_photo(db,rule['id'],show['id'],opened,'photo')
                    db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(time.time(),rule['id']))
        seen.add(signature)
        db.execute('INSERT OR REPLACE INTO alert_initial_state VALUES(?,?)',(rule['id'],signature))

def read(db,alert_id=None):
    saved=rules(db)
    rule=next((r for r in saved if r['id']==alert_id),None) if alert_id else (saved[0] if saved else None)
    return rule['preferences'] if rule else defaults()

def validate(data,preset=False):
    if not isinstance(data,dict):raise ValueError('Invalid settings')
    result=defaults()
    for key in FIELDS:
        values=data.get(key,[])
        if not isinstance(values,list) or len(values)>200 or any(not isinstance(v,str) or not v or len(v)>300 for v in values):raise ValueError('Invalid '+key)
        result[key]=list(dict.fromkeys(values))
    for value in result['dates']:
        if not re.fullmatch(r'\d{8}',value):raise ValueError('Dates must look like 20261231')
        dt.datetime.strptime(value,'%Y%m%d')
    for key in ('time_from','time_to'):
        value=data.get(key,'')
        if value:
            if not isinstance(value,str) or not re.fullmatch(r'\d\d:\d\d',value):raise ValueError('Times must look like 09:30')
            dt.datetime.strptime(value,'%H:%M')
        result[key]=value
    if result['time_from'] and result['time_to'] and result['time_from']>result['time_to']:raise ValueError('End time must be after start time')
    for key in ('new_shows','availability','enabled','once','notify_existing','urgent','open_on_pc'):
        if type(data.get(key,result[key])) is not bool:raise ValueError('Invalid '+key)
        result[key]=data.get(key,result[key])
    interval=data.get('interval_minutes',10)
    if type(interval) is not int or interval not in (10,30,60):raise ValueError('Choose 10, 30 or 60 minutes')
    result['interval_minutes']=interval
    result['row_show']=data.get('row_show','')
    result['rows']=data.get('rows',[])
    if not isinstance(result['row_show'],str) or len(result['row_show'])>150:raise ValueError('Invalid row showtime')
    if not isinstance(result['rows'],list) or len(result['rows'])>200 or any(not isinstance(r,str) or len(r)>300 for r in result['rows']):raise ValueError('Invalid rows')
    if result['row_show'] and not result['rows']:raise ValueError('Load and select at least one row')
    if result['row_show']==ANY_SHOW:
        if len(result['cinemas'])!=1:raise ValueError('Choose exactly one cinema to watch rows at any showtime')
        if not result['movies'] and not preset:raise ValueError('Choose at least one movie to watch rows at any showtime')
    party=data.get('party_size',0)
    if type(party) is not int or not (party==0 or 2<=party<=10):raise ValueError('Choose 2 to 10 seats together, or any open seat')
    result['party_size']=party if result['row_show'] else 0
    minimum=data.get('min_seats',0)
    if type(minimum) is not int or not (minimum==0 or 1<=minimum<=50):raise ValueError('Choose 1 to 50 minimum seats, or off')
    result['min_seats']=minimum
    if result['rows'] and not result['row_show']:raise ValueError('Select a specific showtime for rows')
    if result['enabled'] and (not result['movies'] or (not result['row_show'] and not (result['new_shows'] or result['availability']))):raise ValueError('Select at least one movie and an alert type before enabling')
    return result

def dimensions(show):
    parts=[x.strip() for x in show.get('language','').split('•')]
    return (parts[0] if parts else '',parts[1] if len(parts)>1 else str(show.get('format','')))

def matches(show,p):
    language,format_=dimensions(show)
    for key,value in [('movies',show['movie']),('cinemas',show['venue_code']),('screens',str(show.get('format',''))),('dates',show['date']),('languages',language),('formats',format_)]:
        if key=='formats':
            if p[key] and not any(format_matches(value,v) for v in p[key]):return False
            continue
        if p[key] and (value not in p[key] if key in ('cinemas','dates') else normalized(value) not in {normalized(v) for v in p[key]}):return False
    if p['categories'] and not any(category_matches(c['name'],p) for c in show['categories'].values()):return False
    if p['time_from'] or p['time_to']:
        try:clock=dt.datetime.strptime(str(show.get('time') or '').strip(),'%I:%M %p').strftime('%H:%M')
        except ValueError:return False
        if p['time_from'] and clock<p['time_from']:return False
        if p['time_to'] and clock>p['time_to']:return False
    return True

def suppressed_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS suppressed_outbox AS SELECT *, CAST(0 AS REAL) AS suppressed_at FROM outbox WHERE 0')
    if 'alert_id' not in {row[1] for row in db.execute('PRAGMA table_info(suppressed_outbox)')}:
        try:db.execute("ALTER TABLE suppressed_outbox ADD COLUMN alert_id TEXT DEFAULT ''")
        except sqlite3.OperationalError as error:
            if 'duplicate column name' not in str(error):raise
    return 'id,text,photo,created,delivered,attempts,next_try,alert_id,suppressed_at'

def archive_message(db,message_id):
    """Move one message Telegram keeps rejecting out of the queue, keeping it for inspection."""
    fields=suppressed_table(db)
    db.execute(f'INSERT INTO suppressed_outbox({fields}) SELECT id,text,photo,created,delivered,attempts+1,next_try,alert_id,? FROM outbox WHERE id=? AND delivered=0',(time.time(),message_id))
    db.execute('DELETE FROM outbox WHERE id=? AND delivered=0',(message_id,))

def archive_pending(db,alert_id=None):
    fields=suppressed_table(db)
    if alert_id:
        db.execute(f'INSERT INTO suppressed_outbox({fields}) SELECT id,text,photo,created,delivered,attempts,next_try,alert_id,? FROM outbox WHERE delivered=0 AND alert_id=?',(time.time(),alert_id))
        db.execute('DELETE FROM outbox WHERE delivered=0 AND alert_id=?',(alert_id,))
    else:
        db.execute(f'INSERT INTO suppressed_outbox({fields}) SELECT id,text,photo,created,delivered,attempts,next_try,alert_id,? FROM outbox WHERE delivered=0',(time.time(),))
        db.execute('DELETE FROM outbox WHERE delivered=0')

def save(db,data,alert_id=None,create=False):
    p=validate(data)
    saved=rules(db)
    before=next((r['preferences'] for r in saved if r['id']==alert_id),None) if alert_id and not create else None
    p['open_on_pc_until']=arm_until(db,p['open_on_pc'],before)
    if create:
        alert_id=uuid.uuid4().hex
    elif alert_id:
        if not any(rule['id']==alert_id for rule in saved):raise ValueError('Alert no longer exists')
    elif saved:
        alert_id=saved[0]['id']
    else:alert_id=uuid.uuid4().hex
    if p['row_show'] and p['row_show']!=ANY_SHOW and p['enabled']:
        import row_watch
        layout=row_watch.get(db,p['row_show'])
        show=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(p['row_show'],time.time())).fetchone()
        if not show or not matches(json.loads(show[0]),p):raise ValueError('The row showtime must match your selected filters')
        allowed={r['id'] for r in layout['data'].get('rows',[]) if category_matches(r['category'],p)}
        if not set(p['rows']).issubset(allowed):raise ValueError('Reload the layout and choose valid rows for this category')
        # A source 403 must not invalidate a previously successful layout. Keep
        # allowing alerts for rows from that cached layout while row_watch
        # observes its normal retry cooldown; reject only if no layout was ever
        # read for this showtime.
        if p['enabled'] and not layout['checked']:raise ValueError('Load the seat layout once before enabling row alerts')
        db.commit()
    now=time.time();db.execute('BEGIN IMMEDIATE')
    # Only turning an alert off drops its queue; ordinary edits of an enabled alert keep it.
    if not p['enabled']:archive_pending(db,alert_id)
    db.execute('''INSERT INTO alerts(id,payload,created,updated) VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated,completed=0''',
        (alert_id,json.dumps(p),now,now))
    enabled=bool(db.execute("SELECT 1 FROM alerts WHERE json_extract(payload,'$.enabled')=1 LIMIT 1").fetchone())
    setmeta(db,'notifications_paused','0' if enabled else '1')
    setmeta(db,'notifications_mode','enabled' if enabled else 'paused')
    setmeta(db,'watch_completed','0')
    db.commit()
    return alert_id


def transition(db,alert_id,action):
    rule=next((r for r in rules(db) if r['id']==alert_id),None)
    if not rule:raise ValueError('Alert no longer exists')
    p=rule['preferences']
    if action=='reset':
        if rule['status']=='missed':raise ValueError('Choose a future date before resetting this alert')
        saved=save(db,{**p,'enabled':True},alert_id)
        db.execute('UPDATE alerts SET last_alert=0 WHERE id=?',(alert_id,));db.commit()
        return saved
    if action not in ('cancel','booked'):raise ValueError('Unknown alert action')
    p['enabled']=False;p['request_state']='cancelled' if action=='cancel' else 'booked'
    db.execute('BEGIN IMMEDIATE');archive_pending(db,alert_id)
    db.execute('UPDATE alerts SET payload=?,updated=?,completed=? WHERE id=?',
        (json.dumps(p),time.time(),time.time() if action=='booked' else 0,alert_id))
    enabled=bool(db.execute("SELECT 1 FROM alerts WHERE json_extract(payload,'$.enabled')=1 LIMIT 1").fetchone())
    setmeta(db,'notifications_paused','0' if enabled else '1')
    setmeta(db,'notifications_mode','enabled' if enabled else 'paused');db.commit()
    return alert_id


def delete(db,alert_id):
    if not isinstance(alert_id,str) or not alert_id or len(alert_id)>150:
        raise ValueError('Choose an alert to delete')
    if not any(rule['id']==alert_id for rule in rules(db)):
        raise ValueError('Alert no longer exists')
    db.execute('BEGIN IMMEDIATE')
    archive_pending(db,alert_id)
    db.execute('DELETE FROM alerts WHERE id=?',(alert_id,))
    # Once a user deletes a migrated request, an empty alerts table is real
    # state. Do not recreate it from obsolete single-alert settings on GET.
    setmeta(db,'alerts_migration_complete','1')
    for table in ('row_alert_state','alert_initial_state','pending_seat_photos'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            # A '*' alert keeps one state row per showtime, keyed alert_id:show_id.
            db.execute(f'DELETE FROM {table} WHERE alert_id=? OR substr(alert_id,1,?)=?',(alert_id,len(alert_id)+1,alert_id+':'))
    enabled=bool(db.execute("SELECT 1 FROM alerts WHERE json_extract(payload,'$.enabled')=1 LIMIT 1").fetchone())
    setmeta(db,'notifications_paused','0' if enabled else '1')
    setmeta(db,'notifications_mode','enabled' if enabled else 'paused')
    db.commit()

PHOTO_MARK='📸'  # seat-photo follow-ups start with it, so Open on PC does not reopen the page for them
PHOTO_LIFETIME=1800  # a photo follow-up that could not be sent in 30 minutes is dropped

def rows_enabled():
    """Whether seat maps can be read on this host; without them no seat photo or seat count exists."""
    return os.environ.get('BMS_ROWS_ENABLED')=='1'

def photo_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS pending_seat_photos(alert_id TEXT NOT NULL,show_id TEXT NOT NULL,categories TEXT NOT NULL,mode TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',created REAL NOT NULL,next_read REAL NOT NULL,PRIMARY KEY(alert_id,show_id))")

def queue_seat_photo(db,alert_id,show_id,categories,mode,text='',now=None):
    """Ask for a seat-map follow-up ('photo') or a seat-count gate ('gate') for one alert and showtime.
    A second opening of the same alert and showtime merges into the waiting row."""
    photo_schema(db);now=time.time() if now is None else now
    old=db.execute('SELECT categories,text,mode,created FROM pending_seat_photos WHERE alert_id=? AND show_id=?',(alert_id,show_id)).fetchone()
    if old and old['mode']!=mode:old=None  # the alert was edited between modes: start over
    names=list(dict.fromkeys([*(json.loads(old['categories']) if old else []),*categories]))
    if old and text and old['text'] and text not in old['text']:text=old['text']+chr(10)*2+text
    elif old:text=old['text'] or text
    db.execute('INSERT OR REPLACE INTO pending_seat_photos VALUES(?,?,?,?,?,?,?)',(alert_id,show_id,json.dumps(names),mode,text,now,now))

def gate_waiting(db,alert_id):
    photo_schema(db)
    return bool(db.execute("SELECT 1 FROM pending_seat_photos WHERE alert_id=? AND mode='gate' LIMIT 1",(alert_id,)).fetchone())

def prune_seat_photos(db,now=None):
    """Drop rows whose alert is gone, whose gated alert stopped, whose showtime started, or that waited too long."""
    photo_schema(db);now=time.time() if now is None else now
    for r in db.execute('SELECT alert_id,show_id,mode,created FROM pending_seat_photos').fetchall():
        alert=db.execute('SELECT payload,completed FROM alerts WHERE id=?',(r['alert_id'],)).fetchone()
        cutoff=db.execute('SELECT cutoff FROM sessions WHERE id=? AND active=1',(r['show_id'],)).fetchone()
        if r['mode']=='photo':drop=not alert or now-r['created']>PHOTO_LIFETIME
        else:
            try:p=json.loads(alert[0]) if alert else {}
            except ValueError:p={}
            drop=not alert or not p.get('enabled') or alert[1]
        if drop or not cutoff or cutoff[0]<=now:db.execute('DELETE FROM pending_seat_photos WHERE alert_id=? AND show_id=?',(r['alert_id'],r['show_id']))

def drop_unbookable_gates(db,current):
    """A gated alert stops waiting once none of its categories is bookable in the latest listing."""
    photo_schema(db)
    for show in current.values():
        for r in db.execute("SELECT alert_id,categories FROM pending_seat_photos WHERE mode='gate' AND show_id=?",(show['id'],)).fetchall():
            names={normalized(n) for n in json.loads(r['categories'])}
            if not any(c['status'] in ('1','2','3') and normalized(c['name']) in names for c in show['categories'].values()):
                db.execute("DELETE FROM pending_seat_photos WHERE alert_id=? AND show_id=?",(r['alert_id'],show['id']))

def seat_photo_shows(db,now=None):
    """Showtimes with a follow-up due now: they are read like urgent seat alerts."""
    photo_schema(db);now=time.time() if now is None else now
    return sorted({r[0] for r in db.execute('SELECT show_id FROM pending_seat_photos WHERE next_read<=?',(now,))})

def delivered(db,alert_id=None):
    saved=rules(db)
    rule=next((r for r in saved if r['id']==alert_id),None) if alert_id else (saved[0] if saved else None)
    if not rule:return
    p=rule['preferences']
    # One check can queue several messages (a cinema each, or 2800-char chunks); finish only after the last is delivered.
    if p['once'] and not db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(rule['id'],)).fetchone() and not gate_waiting(db,rule['id']):
        p['enabled']=False
        db.execute('UPDATE alerts SET payload=?,updated=?,completed=? WHERE id=?',
            (json.dumps(p),time.time(),time.time(),rule['id']))
        archive_pending(db,rule['id'])
        enabled=bool(db.execute("SELECT 1 FROM alerts WHERE json_extract(payload,'$.enabled')=1 LIMIT 1").fetchone())
        setmeta(db,'notifications_paused','0' if enabled else '1')
        setmeta(db,'notifications_mode','enabled' if enabled else 'paused')
        setmeta(db,'watch_completed',str(time.time()))

def options(db):
    from api_source import seat_layout_url
    def safe_link(s):
        # One stored show with an odd event code must not break the dashboard for everyone.
        try:return seat_layout_url(s)
        except Exception:return ''
    shows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM sessions WHERE active=1 AND cutoff>?',(time.time(),))]
    result={k:set() for k in FIELDS};cinemas={};profiles=set()
    for s in shows:
        language,format_=dimensions(s)
        for k,v in [('movies',s['movie']),('screens',str(s.get('format',''))),('languages',language),('formats',format_),('dates',s['date'])]:
            if v:result[k].add(v)
        cinemas[s['venue_code']]=s['venue']
        result['categories'].update(c['name'] for c in s['categories'].values())
        profiles.add((s['movie'],s['venue_code'],language,format_,tuple(sorted(c['name'] for c in s['categories'].values())),str(s.get('format','')),s['date'],s['url']))
    result['movies'].update(r[0] for r in db.execute('SELECT title FROM inventory'))
    cinema_names={r['code']:r['name'] for r in db.execute('SELECT code,name FROM venues')}
    cinema_names.update(cinemas)
    venue_options={code:{'screens':set(VENUE_SCREENS.get(code,[])),'categories':set()} for code in cinema_names}
    for row in db.execute('SELECT payload FROM sessions'):
        s=json.loads(row[0]);v=venue_options.setdefault(s['venue_code'],{'screens':set(),'categories':set()})
        if s.get('format'):v['screens'].add(str(s['format']))
        v['categories'].update(c['name'] for c in s['categories'].values())
    for v in venue_options.values():
        result['screens'].update(v['screens']);result['categories'].update(v['categories'])
    result['languages'].update(['Telugu','Hindi','Tamil','Malayalam','Kannada','English'])
    result['formats'].update(['2D','3D','IMAX','IMAX 3D','4DX','4DX 3D'])
    category_rows={}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='row_layouts'").fetchone():
        for row in db.execute("SELECT show_id,payload FROM row_layouts WHERE checked>0"):
            try:category_rows[row[0]]=[{'category':r['category'],'row':r['row']} for r in json.loads(row[1]).get('rows',[])]
            except (ValueError,KeyError,TypeError):continue
    return {'category_rows':category_rows,**{k:sorted(v) for k,v in result.items()},'cinemas':[{'value':k,'label':v} for k,v in sorted(cinema_names.items(),key=lambda x:x[1])],
            'cinema_names':cinema_names,
            'venue_options':{code:{k:sorted(items) for k,items in value.items()} for code,value in venue_options.items()},
            'profiles':[{'movie':p[0],'cinema':p[1],'language':p[2],'format':p[3],'categories':p[4],'screen':p[5],'date':p[6],'url':p[7]} for p in sorted(profiles)],
            'showtimes':[{'id':s['id'],'movie':s['movie'],'cinema':s['venue_code'],'screen':str(s.get('format','')),'language':dimensions(s)[0],'format':dimensions(s)[1],'date':s['date'],'time':s['time'],'categories':[c['name'] for c in s['categories'].values()],'booking_url':safe_link(s)} for s in shows]}


PRESET_LIMIT=30

def presets(db):
    try:saved=json.loads(meta(db,'alert_presets','[]'))
    except ValueError:return []
    return [x for x in saved if isinstance(x,dict)] if isinstance(saved,list) else []

def preset_preferences(data):
    """Validated filters for a preset: no movies or dates, never enabled, a row_show kept only as '*'."""
    if not isinstance(data,dict):raise ValueError('Invalid settings')
    any_show=data.get('row_show')==ANY_SHOW
    clean={**data,'movies':[],'dates':[],'enabled':False,'request_state':'','open_on_pc':False,'open_on_pc_until':0}
    if not any_show:clean.update(row_show='',rows=[],party_size=0)
    return validate(clean,preset=True)

def save_preset(db,name,data):
    if not isinstance(name,str) or not 1<=len(name.strip())<=40:raise ValueError('Preset name must be 1 to 40 characters')
    name=name.strip();p=preset_preferences(data);saved=presets(db)
    same=next((i for i,x in enumerate(saved) if str(x.get('name','')).casefold()==name.casefold()),None)
    if same is None and len(saved)>=PRESET_LIMIT:raise ValueError('You can keep up to 30 presets. Delete one first')
    item={'id':saved[same]['id'] if same is not None else uuid.uuid4().hex,'name':name,'preferences':p}
    if same is None:saved.append(item)
    else:saved[same]=item
    setmeta(db,'alert_presets',json.dumps(saved));db.commit()
    return saved

def delete_preset(db,preset_id):
    if not isinstance(preset_id,str) or not preset_id:raise ValueError('Choose a preset to delete')
    saved=presets(db);kept=[x for x in saved if x.get('id')!=preset_id]
    if len(kept)==len(saved):raise ValueError('Preset no longer exists')
    setmeta(db,'alert_presets',json.dumps(kept));db.commit()
    return kept

def history_summary(text,row):
    lines=[l.strip() for l in str(text or '').split('\n')]
    if row:line=next((l for l in lines if l),'')
    else:
        # After the header (movie, language, date, venue) the first line of the form "Category now available · time · seats".
        body=lines[next((i for i,l in enumerate(lines) if not l),len(lines)):]
        line=next((l for l in body if ' \u00b7 ' in l),'') or next((l for l in lines if l),'')
    return line[:140]

def history(db,limit=10):
    """Last `limit` queued messages per alert in one query, newest first: {alert_id:[{created,delivered,summary,url}]}."""
    rows=db.execute('''SELECT alert_id,created,delivered,text FROM (SELECT alert_id,created,delivered,text,
        row_number() OVER (PARTITION BY alert_id ORDER BY created DESC) AS n FROM outbox WHERE alert_id NOT IN ('','__health__'))
        WHERE n<=? ORDER BY alert_id,created DESC''',(limit,)).fetchall()
    kinds={r['id']:bool(json.loads(r['payload']).get('row_show')) for r in db.execute('SELECT id,payload FROM alerts')}
    result={}
    for r in rows:
        links=re.findall(r'https://in\.bookmyshow\.com/\S+',r['text'] or '')
        result.setdefault(r['alert_id'],[]).append({'created':r['created'],'delivered':r['delivered'] or 0,
            'summary':history_summary(r['text'],kinds.get(r['alert_id'],False)),'url':links[-1] if links else ''})
    return result

PC_WINDOW=600
PC_ARM=3*3600  # Open on PC stays armed for 3 hours
PC_SEEN=120  # a PC client counts as live for 2 minutes after its last poll
def pc_clients(db,now=None):
    """Seconds since the Chrome extension last polled, or None: {extension}."""
    now=time.time() if now is None else now;seen={}
    for name in ('extension',):
        at=float(meta(db,f'pc_{name}_seen','0') or 0);seen[name]=round(max(0,now-at),1) if at else None
    return seen
def arm_until(db,on,before=None,now=None):
    """Expiry for an Open on PC switch: kept while it stays on, a fresh 3 hours when it turns on (needs a live PC client), 0 when off."""
    now=time.time() if now is None else now
    if not on:return 0
    if before and before.get('open_on_pc') and before.get('open_on_pc_until',0)>now:return before['open_on_pc_until']
    if not any(v is not None and v<=PC_SEEN for v in pc_clients(db,now).values()):raise ValueError('Enable the Screenwatch extension in Chrome on this PC first')
    return now+PC_ARM
def expire_arms(db,now=None):
    """An expired (or legacy, expiry-less) arm is off: clear it in storage."""
    now=time.time() if now is None else now;changed=False
    for r in db.execute("SELECT id,payload FROM alerts WHERE json_extract(payload,'$.open_on_pc')=1").fetchall():
        p=json.loads(r['payload'])
        if p.get('open_on_pc_until',0)<=now:
            p['open_on_pc']=False;p['open_on_pc_until']=0;db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),r['id']));changed=True
    if changed:db.commit()

def pc_show(db,url):
    """Movie, venue, date, time and screen of the showtime a seat-layout link points at, or None."""
    m=re.search(r'/seat-layout/([A-Z0-9]+)/([A-Z0-9]+)/([A-Za-z0-9]+)/(\d{8})$',url)
    row=db.execute('SELECT payload FROM sessions WHERE id=?',(f'{m[2]}:{m[4]}:{m[3]}:{m[1]}',)).fetchone() if m else None
    if not row:return None
    s=json.loads(row[0])
    return {'movie':s.get('movie',''),'venue':s.get('venue',''),'date':s.get('date',''),'time':s.get('time',''),'screen':s.get('format','')}

def pc_link(text):
    """The one BookMyShow link the extension opens: the seat layout when a message has several, else the last."""
    links=re.findall(r'https://in\.bookmyshow\.com/[^\s<>"\x27]+',text or '')
    return next((l for l in links if '/seat-layout/' in l),links[-1] if links else '')

def pc_seat_pair(text,url):
    """Return an exact show and pair only when the alert explicitly names both."""
    if not re.fullmatch(
        r'https://in\.bookmyshow\.com/movies/hyderabad/seat-layout/ET[A-Z0-9]{6,}/[A-Z0-9]+/[A-Za-z0-9]+/\d{8}',url
    ):
        return None
    match=re.search(r'^⭐ Best 2 together: ([^\n]+) / ([^\n]+) · seats ([0-9]{1,4}), ([0-9]{1,4})$',text or '',re.MULTILINE)
    if not match:
        return None
    category,row,left,right=match.groups()
    if abs(int(left)-int(right))!=1:
        return None
    return {'category':category,'row':row,'seats':[left,right]}

def pc_alerts(db,since,now=None,client=''):
    """Queued messages of alerts with Open on PC on, newer than `since`, for the Chrome extension: {armed,now,extension,armed_alerts,events}."""
    now=time.time() if now is None else now;ensure(db);expire_arms(db,now)
    if client=='extension':setmeta(db,f'pc_{client}_seen',now);db.commit()
    on={};armed=False;listed=[]
    for r in db.execute('SELECT id,payload FROM alerts'):
        try:p=json.loads(r['payload'])
        except ValueError:continue
        if p.get('open_on_pc'):on[r['id']]=bool(p.get('row_show'));armed=armed or bool(p.get('enabled'));listed.append({'id':r['id'],'movie':', '.join(p.get('movies',[])),'until':p.get('open_on_pc_until',0)})
    events=[]
    if on:
        for r in db.execute(f"SELECT id,alert_id,created,text FROM outbox WHERE created>? AND created>? AND text NOT LIKE '{PHOTO_MARK}%' AND alert_id IN ({','.join('?'*len(on))}) ORDER BY created",(since,now-PC_WINDOW,*on)):
            url=pc_link(r['text'])
            if url:
                event={'id':r['id'],'alert':r['alert_id'],'created':r['created'],'summary':history_summary(r['text'],on[r['alert_id']]),'url':url}
                pair=pc_seat_pair(r['text'],url) if on[r['alert_id']] else None
                if pair:event['pair']=pair
                show=pc_show(db,url)
                if show:event['show']=show
                events.append(event)
            if len(events)>=5:break
    seen=pc_clients(db,now)['extension']
    return {'armed':armed,'now':now,'extension':seen is not None and seen<=PC_SEEN,'armed_alerts':listed,'events':events}

def set_open_on_pc(db,alert_id,value=False):
    """Flip only open_on_pc on a stored alert; nothing else is re-validated or changed."""
    row=db.execute('SELECT payload FROM alerts WHERE id=?',(alert_id,)).fetchone() if isinstance(alert_id,str) else None
    if not row:raise ValueError('Alert no longer exists')
    p=json.loads(row[0]);p['open_on_pc_until']=arm_until(db,bool(value),p);p['open_on_pc']=bool(value)
    db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),alert_id));db.commit()
