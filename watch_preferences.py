"""Saved personal watch filters and conservative Telegram pacing."""
import datetime as dt
import json
import time
from store import meta,setmeta

FIELDS=('movies','cinemas','screens','languages','formats','dates','categories')
def defaults():
    return {**{k:[] for k in FIELDS},'time_from':'','time_to':'','new_shows':True,'availability':False,'enabled':False,'once':True,'interval_minutes':10,'row_show':'','rows':[]}

def read(db):
    try:return {**defaults(),**json.loads(meta(db,'watch_preferences','{}'))}
    except (ValueError,TypeError):return defaults()

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
    if result['enabled'] and (not result['movies'] or not (result['new_shows'] or result['availability'])):raise ValueError('Select at least one movie and an alert type before enabling')
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

def archive_pending(db):
    db.execute('CREATE TABLE IF NOT EXISTS suppressed_outbox AS SELECT *, CAST(0 AS REAL) AS suppressed_at FROM outbox WHERE 0')
    db.execute('INSERT INTO suppressed_outbox SELECT *,? FROM outbox WHERE delivered=0',(time.time(),))
    db.execute('DELETE FROM outbox WHERE delivered=0')

def save(db,data):
    p=validate(data)
    if p['row_show']:
        import row_watch
        layout=row_watch.get(db,p['row_show'])
        show=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(p['row_show'],time.time())).fetchone()
        if not show or not matches(json.loads(show[0]),p):raise ValueError('The row showtime must match your selected filters')
        allowed={r['id'] for r in layout['data'].get('rows',[]) if not p['categories'] or r['category'] in p['categories']}
        if not set(p['rows']).issubset(allowed):raise ValueError('Reload the layout and choose valid rows for this category')
        if p['enabled'] and (layout['error'] or time.time()-layout['checked']>300):raise ValueError('Reload the seat layout before enabling row alerts')
        db.commit()
    db.execute('BEGIN IMMEDIATE')
    archive_pending(db)
    setmeta(db,'watch_preferences',json.dumps(p))
    setmeta(db,'notifications_paused','0' if p['enabled'] else '1')
    setmeta(db,'notifications_mode','enabled' if p['enabled'] else 'paused')
    setmeta(db,'watch_completed','0')
    db.commit()
    return p

def delivered(db):
    p=read(db)
    if p['once']:
        p['enabled']=False
        setmeta(db,'watch_preferences',json.dumps(p))
        setmeta(db,'notifications_paused','1')
        setmeta(db,'notifications_mode','paused')
        setmeta(db,'watch_completed',str(time.time()))
        archive_pending(db)

def options(db):
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
    return {**{k:sorted(v) for k,v in result.items()},'cinemas':[{'value':k,'label':v} for k,v in sorted(cinemas.items(),key=lambda x:x[1])],
            'profiles':[{'movie':p[0],'cinema':p[1],'language':p[2],'format':p[3],'categories':p[4],'screen':p[5],'date':p[6],'url':p[7]} for p in sorted(profiles)],
            'showtimes':[{'id':s['id'],'movie':s['movie'],'cinema':s['venue_code'],'screen':str(s.get('format','')),'language':dimensions(s)[0],'format':dimensions(s)[1],'date':s['date'],'time':s['time'],'categories':[c['name'] for c in s['categories'].values()]} for s in shows]}
