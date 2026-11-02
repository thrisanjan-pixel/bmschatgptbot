"""Saved personal watch filters and conservative Telegram pacing."""
import datetime as dt
import json
import re
import sqlite3
import time
import uuid
from store import emit,meta,setmeta

FIELDS=('movies','cinemas','screens','languages','formats','dates','categories')
VENUE_SCREENS={
    'PRHN':['PCX SCREEN','PCX HDR BY BARCO','BARCO FLAGSHIP 4K LASER','BARCO 4K LASER',
            'BARCO 4K SMART LASER','PCX Infinity Vis 3D','Infinity Vision 3D','Infinity Vision 2D'],
    'AMBH':['BARCO FLAGSHIP LASER DOLBY ATMOS','M B LUXE','VIP SCREEN','HDR BY BARCO'],
    'ALUC':['DOLBY CINEMA'],
}

def normalized(value):return ' '.join(str(value).casefold().split())
def category_matches(name,p):return not p['categories'] or normalized(name) in {normalized(v) for v in p['categories']}
def format_matches(actual,wanted):
    actual=normalized(actual);wanted=normalized(wanted)
    if wanted in ('2d','3d'):return bool(re.search(r'(?<![a-z0-9])'+wanted+r'(?![a-z0-9])',actual))
    if wanted in ('imax','4dx'):return wanted in actual
    return actual==wanted
def defaults():
    return {**{k:[] for k in FIELDS},'time_from':'','time_to':'','new_shows':True,'availability':True,'enabled':False,'once':True,'notify_existing':True,'interval_minutes':10,'row_show':'','rows':[],'request_state':''}

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
    ensure(db)
    result=[]
    for row in db.execute('SELECT * FROM alerts ORDER BY created DESC'):
        rule={**dict(row),'preferences':{**defaults(),**json.loads(row['payload'])}}
        p=rule['preferences'];state=p.get('request_state','')
        expired=False
        if p['dates']:
            from api_source import end_day
            expired=max(end_day(day) for day in p['dates'])<=time.time()
        if p['row_show']:
            show=db.execute('SELECT cutoff FROM sessions WHERE id=?',(p['row_show'],)).fetchone()
            expired=expired or bool(show and show[0]<=time.time())
        rule['status']='cancelled' if state=='cancelled' else 'completed' if rule['completed'] or state=='booked' else 'missed' if expired and p['enabled'] else 'active' if p['enabled'] else 'paused'
        message=db.execute('SELECT text FROM outbox WHERE alert_id=? ORDER BY created DESC LIMIT 1',(rule['id'],)).fetchone()
        link=re.search(r'https://in\.bookmyshow\.com/movies/hyderabad/seat-layout/[A-Za-z0-9/]+',message[0]) if message else None
        rule['booking_url']=link[0] if link else ''
        checked=0;error='';due=0
        if p['row_show'] and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='row_layouts'").fetchone():
            layout=db.execute('SELECT checked,error,due FROM row_layouts WHERE show_id=?',(p['row_show'],)).fetchone()
            if layout:checked,error,due=layout
        elif not p['row_show']:
            jobs=[r for r in db.execute('SELECT payload,checked,error,due FROM jobs') if json.loads(r['payload']).get('movie') in p['movies']]
            checked=max((r['checked'] for r in jobs),default=0)
            error=next((r['error'] for r in jobs if r['error']),'')
            due=min((r['due'] for r in jobs),default=0)
        delivery=db.execute('SELECT count(*),coalesce(max(attempts),0) FROM outbox WHERE alert_id=? AND delivered=0',(rule['id'],)).fetchone()
        health='Source unavailable' if error else 'Checks delayed' if checked and time.time()-checked>180 else 'Watching seats' if p['row_show'] else 'Watching availability' if checked else 'Waiting for listings'
        rule['health']={'label':health if rule['status']=='active' else rule['status'].capitalize(),'last_success':checked,'error':error,'next_check':due,'pending_messages':delivery[0],'delivery_attempts':delivery[1]}
        keys=[p['row_show']] if p['row_show'] else [r['id'] for r in db.execute('SELECT id,payload FROM jobs') if json.loads(r['payload']).get('movie') in p['movies']]
        source='rows' if p['row_show'] else 'listings'
        intervals=[]
        for key in keys:
            intervals.extend(r[0] for r in db.execute('SELECT gap FROM check_intervals WHERE source=? AND job_key=? AND at>=?',(source,key,max(rule['created'],time.time()-7*86400))))
        delays=db.execute('SELECT avg(delivered-created),max(delivered-created),count(*) FROM outbox WHERE alert_id=? AND delivered>0',(rule['id'],)).fetchone()
        rule['report']={'window_days':7,'gap_samples':len(intervals),'average_gap':sum(intervals)/len(intervals) if intervals else None,'longest_gap':max(intervals) if intervals else None,'delivery_average':delays[0],'delivery_longest':delays[1],'deliveries':delays[2],'last_success':checked}
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
                opened=sorted({category['name'] for category in show['categories'].values()
                    if category['status'] in ('1','2','3') and
                    category_matches(category['name'],p)})
                if opened:found.append((show,opened))
            if found and not db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(rule['id'],)).fetchone():
                lines=[]
                for show,opened in found[:3]:
                    lines.append(show['venue']+' · '+show['date']+' · '+show['time']+'\n'+
                        ', '.join(opened)+'\n'+seat_layout_url(show))
                extra=f'\n\n{len(found)-3} more matching showtimes are open.' if len(found)>3 else ''
                message='🎬 '+found[0][0]['movie']+'\nSeats already available for this alert:\n\n'+\
                    '\n\n'.join(lines)+extra
                emit(db,'initial:'+rule['id']+':'+signature,message[:3900],alert_id=rule['id'])
                db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(time.time(),rule['id']))
        seen.add(signature)
        db.execute('INSERT OR REPLACE INTO alert_initial_state VALUES(?,?)',(rule['id'],signature))

def read(db,alert_id=None):
    saved=rules(db)
    rule=next((r for r in saved if r['id']==alert_id),None) if alert_id else (saved[0] if saved else None)
    return rule['preferences'] if rule else defaults()

def validate(data):
    if not isinstance(data,dict):raise ValueError('Invalid settings')
    result=defaults()
    for key in FIELDS:
        values=data.get(key,[])
        if not isinstance(values,list) or len(values)>200 or any(not isinstance(v,str) or not v or len(v)>300 for v in values):raise ValueError('Invalid '+key)
        result[key]=list(dict.fromkeys(values))
    for value in result['dates']:dt.datetime.strptime(value,'%Y%m%d')
    for key in ('time_from','time_to'):
        value=data.get(key,'')
        if value:dt.datetime.strptime(value,'%H:%M')
        result[key]=value
    if result['time_from'] and result['time_to'] and result['time_from']>result['time_to']:raise ValueError('End time must be after start time')
    for key in ('new_shows','availability','enabled','once','notify_existing'):
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
        if p[key] and (value not in p[key] if key in ('movies','cinemas','dates') else normalized(value) not in {normalized(v) for v in p[key]}):return False
    if p['categories'] and not any(category_matches(c['name'],p) for c in show['categories'].values()):return False
    if p['time_from'] or p['time_to']:
        try:clock=dt.datetime.strptime(show['time'].strip(),'%I:%M %p').strftime('%H:%M')
        except ValueError:return False
        if p['time_from'] and clock<p['time_from']:return False
        if p['time_to'] and clock>p['time_to']:return False
    return True

def archive_pending(db,alert_id=None):
    db.execute('CREATE TABLE IF NOT EXISTS suppressed_outbox AS SELECT *, CAST(0 AS REAL) AS suppressed_at FROM outbox WHERE 0')
    if 'alert_id' not in {row[1] for row in db.execute('PRAGMA table_info(suppressed_outbox)')}:
        try:db.execute("ALTER TABLE suppressed_outbox ADD COLUMN alert_id TEXT DEFAULT ''")
        except sqlite3.OperationalError as error:
            if 'duplicate column name' not in str(error):raise
    fields='id,text,photo,created,delivered,attempts,next_try,alert_id,suppressed_at'
    if alert_id:
        db.execute(f'INSERT INTO suppressed_outbox({fields}) SELECT id,text,photo,created,delivered,attempts,next_try,alert_id,? FROM outbox WHERE delivered=0 AND alert_id=?',(time.time(),alert_id))
        db.execute('DELETE FROM outbox WHERE delivered=0 AND alert_id=?',(alert_id,))
    else:
        db.execute(f'INSERT INTO suppressed_outbox({fields}) SELECT id,text,photo,created,delivered,attempts,next_try,alert_id,? FROM outbox WHERE delivered=0',(time.time(),))
        db.execute('DELETE FROM outbox WHERE delivered=0')

def save(db,data,alert_id=None,create=False):
    p=validate(data)
    saved=rules(db)
    if create:
        alert_id=uuid.uuid4().hex
    elif alert_id:
        if not any(rule['id']==alert_id for rule in saved):raise ValueError('Alert no longer exists')
    elif saved:
        alert_id=saved[0]['id']
    else:alert_id=uuid.uuid4().hex
    if p['row_show'] and p['enabled']:
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
    archive_pending(db,alert_id)
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
    for table in ('row_alert_state','alert_initial_state'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            db.execute(f'DELETE FROM {table} WHERE alert_id=?',(alert_id,))
    enabled=bool(db.execute("SELECT 1 FROM alerts WHERE json_extract(payload,'$.enabled')=1 LIMIT 1").fetchone())
    setmeta(db,'notifications_paused','0' if enabled else '1')
    setmeta(db,'notifications_mode','enabled' if enabled else 'paused')
    db.commit()

def delivered(db,alert_id=None):
    saved=rules(db)
    rule=next((r for r in saved if r['id']==alert_id),None) if alert_id else (saved[0] if saved else None)
    if not rule:return
    p=rule['preferences']
    if p['once']:
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
            'showtimes':[{'id':s['id'],'movie':s['movie'],'cinema':s['venue_code'],'screen':str(s.get('format','')),'language':dimensions(s)[0],'format':dimensions(s)[1],'date':s['date'],'time':s['time'],'categories':[c['name'] for c in s['categories'].values()],'booking_url':seat_layout_url(s)} for s in shows]}
