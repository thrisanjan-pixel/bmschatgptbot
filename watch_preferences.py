"""Saved personal watch filters and conservative Telegram pacing."""
import datetime as dt
import json
import sqlite3
import time
import uuid
from store import emit,meta,setmeta

FIELDS=('movies','cinemas','screens','languages','formats','dates','categories')
def defaults():
    return {**{k:[] for k in FIELDS},'time_from':'','time_to':'','new_shows':True,'availability':False,'enabled':False,'once':True,'interval_minutes':10,'row_show':'','rows':[]}

def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS alerts(
        id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL,
        updated REAL NOT NULL,last_alert REAL NOT NULL DEFAULT 0,
        completed REAL NOT NULL DEFAULT 0)''')
    if db.execute('SELECT 1 FROM alerts LIMIT 1').fetchone():return
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
    return [{**dict(row),'preferences':{**defaults(),**json.loads(row['payload'])}}
        for row in db.execute('SELECT * FROM alerts ORDER BY created DESC')]

def active(db):
    return [rule for rule in rules(db) if rule['preferences']['enabled']]


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
        signature=json.dumps({key:p[key] for key in (*FIELDS,'time_from','time_to','availability')},sort_keys=True)
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
        if signature not in seen:
            found=[]
            for show in shows:
                if not matches(show,p):continue
                opened=sorted({category['name'] for category in show['categories'].values()
                    if category['status'] in ('1','2','3') and
                    (not p['categories'] or category['name'] in p['categories'])})
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
    for key in ('new_shows','availability','enabled','once'):
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
        if p[key] and value not in p[key]:return False
    if p['categories'] and not any(c['name'] in p['categories'] for c in show['categories'].values()):return False
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
        if len(saved)>=20:raise ValueError('Maximum 20 alerts. Pause or edit an existing alert.')
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
        allowed={r['id'] for r in layout['data'].get('rows',[]) if not p['categories'] or r['category'] in p['categories']}
        if not set(p['rows']).issubset(allowed):raise ValueError('Reload the layout and choose valid rows for this category')
        if p['enabled'] and (layout['error'] or time.time()-layout['checked']>300):raise ValueError('Reload the seat layout before enabling row alerts')
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
    return {**{k:sorted(v) for k,v in result.items()},'cinemas':[{'value':k,'label':v} for k,v in sorted(cinemas.items(),key=lambda x:x[1])],
            'cinema_names':cinema_names,
            'profiles':[{'movie':p[0],'cinema':p[1],'language':p[2],'format':p[3],'categories':p[4],'screen':p[5],'date':p[6],'url':p[7]} for p in sorted(profiles)],
            'showtimes':[{'id':s['id'],'movie':s['movie'],'cinema':s['venue_code'],'screen':str(s.get('format','')),'language':dimensions(s)[0],'format':dimensions(s)[1],'date':s['date'],'time':s['time'],'categories':[c['name'] for c in s['categories'].values()],'booking_url':seat_layout_url(s)} for s in shows]}
