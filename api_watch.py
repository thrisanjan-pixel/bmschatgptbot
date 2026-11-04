"""One paced HTTP worker, durable state and Telegram delivery. No browser processes."""
import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
from pathlib import Path
import signal
import threading
import time
from api_source import CATALOG,ReadError,end_day,get_json,movies,parse,seat_layout_url,show_url,today
from local_credentials import load
from store import connect,coverage,emit,enqueue,meta,setmeta,record_check
from telegram import drain,send
import watch_preferences as preferences

def schedule(db,event,day,title,notify=False):
    url=show_url(event,day)
    if db.execute('SELECT 1 FROM jobs WHERE id=?',('discovery:'+url,)).fetchone():return
    enqueue(db,'discovery',url,{'event':event,'date':day,'movie':title,'notify_first':notify},end_day(day) if day else 0)

def register_movie(db, url, title=None):
    from urllib.parse import urlsplit
    parts = urlsplit(url.strip())
    match = re.fullmatch(r'/movies/hyderabad/([a-zA-Z0-9-]+)/(ET[0-9]+)/*', parts.path)
    if parts.scheme != 'https' or parts.netloc != 'in.bookmyshow.com' or not match:
        raise ValueError('Paste a Hyderabad BookMyShow movie page link')
    slug, event = match.groups()
    canonical = f'https://in.bookmyshow.com/movies/hyderabad/{slug}/{event}'
    existing = db.execute('SELECT title FROM inventory WHERE url=?', (canonical,)).fetchone()
    title = existing[0] if existing else (title or slug.replace('-', ' ').title())
    db.execute('INSERT OR IGNORE INTO inventory VALUES(?,?,?)', (canonical, title, time.time()))
    schedule(db, event, '', title, notify=True)
    return title

def ingest(db,job,data):
    args=json.loads(job['payload']);result=parse(data,args['event'],args['date'])
    key='api:'+args['event']+':'+result['day'];row=db.execute('SELECT payload FROM snapshots WHERE id=?',(key,)).fetchone()
    previous=json.loads(row[0]) if row else None;current=result['shows']
    title=result['film'] or args['movie']
    for show in current.values():show['movie']=show['movie'] or title
    changes={}
    if (previous is not None or args.get('notify_first')) and meta(db,'notifications_paused','0')!='1':
        old=previous or {}
        for rule in preferences.active(db):
            prefs=rule['preferences']
            if prefs.get('row_show'):continue
            groups={}
            for identity,show in current.items():
                if not preferences.matches(show,prefs) and not preferences.matches({**show,'movie':args['movie']},prefs):continue
                change=None
                if identity not in old and prefs['new_shows']:
                    change='New show · '+show['time']
                elif identity in old and prefs['availability']:
                    before=old[identity].get('categories',{})
                    opened=[c['name'] for k,c in show['categories'].items()
                        if k in before and before[k]['status']=='0' and c['status'] in ('1','2','3')
                        and preferences.category_matches(c['name'],prefs)]
                    if opened:change='Category now available · '+show['time']+' · '+', '.join(opened)
                if change:
                    group=groups.setdefault(show['venue_code'],{'show':show,'lines':[]})
                    group['lines'].append((change,seat_layout_url(show)))
            if groups:changes[rule['id']]=(rule,groups)
    if previous!=current:
        revision=int(meta(db,'revision:'+key,'0'))+1;setmeta(db,'revision:'+key,revision)
        if changes:
            date=dt.datetime.strptime(result['day'],'%Y%m%d').strftime('%d %b %Y')
            for alert_id,(rule,groups) in changes.items():
                prefs=rule['preferences']
                for venue,group in groups.items():
                    show=group['show']
                    heading=f"🎬 {title}\n{show['language']}\n📅 {date}\n📍 {show['venue']}\n\n"
                    chunks=['']
                    for line,url in group['lines']:
                        block=line+'\n'+url+'\n'
                        if len(chunks[-1])+len(block)>2800:chunks.append('')
                        chunks[-1]+=block
                    for index,chunk in enumerate(chunks):
                        now=time.time()
                        pending=db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(alert_id,)).fetchone()
                        if now-rule['last_alert']>=prefs['interval_minutes']*60 and (not pending or prefs['interval_minutes']==0):
                            emit(db,f'{alert_id}:{key}:{revision}:{venue}:{index}',heading+chunk,alert_id=alert_id)
                            db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,alert_id))
                            rule['last_alert']=now
                        else:setmeta(db,'watch_skipped',int(meta(db,'watch_skipped','0'))+1)
    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)',(key,json.dumps(current)))
    for old in db.execute('SELECT id,payload FROM sessions WHERE active=1').fetchall():
        show=json.loads(old['payload'])
        if show['event']==args['event'] and show['date']==result['day'] and old['id'] not in current:
            db.execute('UPDATE sessions SET active=0 WHERE id=?',(old['id'],))
    for show in current.values():
        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,1)',(show['id'],json.dumps(show),show['cutoff']))
        db.execute('INSERT INTO venues VALUES(?,?,?,1) ON CONFLICT(code) DO UPDATE SET name=excluded.name,url=excluded.url',
            (show['venue_code'],show['venue'],show['url']))
    for day in result['dates']:schedule(db,args['event'],day,title,previous is not None or args['notify_first'])
    for variant in result['variants']:schedule(db,variant,'',title,args['notify_first'])
    setmeta(db,'api_last_success',time.time())

def seed(db,previous_db=None):
    enqueue(db,'catalog',CATALOG,{})
    register_movie(db,'https://in.bookmyshow.com/movies/hyderabad/ene-repeat/ET00506990','ENE Repeat')
    if previous_db and Path(previous_db).exists() and not meta(db,'imported_inventory'):
        old=connect(previous_db)
        try:
            for row in old.execute('SELECT * FROM inventory'):
                event=row['url'].rsplit('/',1)[-1]
                if event.startswith('ET'):schedule(db,event,'',row['title'])
            for row in old.execute('SELECT payload FROM sessions WHERE active=1 AND cutoff>?',(time.time(),)):
                show=json.loads(row[0]);schedule(db,show['event'],'',show['movie'])
        finally:old.close()
        setmeta(db,'imported_inventory','1')
    db.commit()

def poll_interval(db,job):
    if job['kind']=='catalog':return 600
    movie=json.loads(job['payload']).get('movie','')
    return 30 if any(preferences.normalized(movie) in {preferences.normalized(m) for m in rule['preferences']['movies']} for rule in preferences.active(db)) else 120

def next_job(db,now):
    movies=sorted({preferences.normalized(m) for r in preferences.active(db) for m in r['preferences']['movies']})
    slots=','.join('?' for _ in movies) or "''"
    return db.execute(f"""SELECT * FROM jobs WHERE due<=? AND (expires=0 OR expires>?)
      ORDER BY CASE WHEN due>0 AND due<? THEN 0 WHEN lower(trim(json_extract(payload,'$.movie'))) IN ({slots}) THEN 1 ELSE 2 END,due,checked LIMIT 1""",(now,now,now-300,*movies)).fetchone()

class Monitor:
    def __init__(self,path,dry_run=False):self.path=path;self.dry_run=dry_run;self.stop=threading.Event()
    def worker(self):
        db=connect(self.path)
        try:
            while not self.stop.is_set():
                cooldown=float(meta(db,'source_cooldown','0'))
                if cooldown>time.time():self.stop.wait(min(1,cooldown-time.time()));continue
                now=time.time()
                job=next_job(db,now)
                if not job:self.stop.wait(1);continue
                try:
                    started=time.time();data=get_json(job['url'])
                    if job['kind']=='catalog':
                        listings=movies(data);initialized=bool(meta(db,'catalog_initialized'))
                        for movie in listings:
                            db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?,?)',(movie['url'],movie['title'],now))
                            setmeta(db,'poster:'+movie['url'],movie['image'])
                            schedule(db,movie['event'],'',movie['title'],initialized)
                        setmeta(db,'catalog_initialized','1');setmeta(db,'catalog_last_success',now)
                    else:ingest(db,job,data)
                    db.execute("UPDATE jobs SET checked=?,due=?,error='' WHERE id=?",(time.time(),time.time()+poll_interval(db,job),job['id']))
                    record_check(db,'listings',job['id'],started)
                    db.commit();logging.info('%s checked',job['kind'])
                except Exception as error:
                    db.rollback();delay=max(300,getattr(error,'retry',300))
                    message=str(error) if isinstance(error,ReadError) else type(error).__name__
                    db.execute('UPDATE jobs SET due=?,error=? WHERE id=?',(time.time()+delay,message,job['id']))
                    if getattr(error,'status',0) in (403,429) or getattr(error,'status',0)>=500:
                        started=time.time()
                        setmeta(db,'source_cooldown',started+delay)
                        setmeta(db,'source_cooldown_started',started)
                        setmeta(db,'source_cooldown_duration',delay)
                    record_check(db,'listings',job['id'],now,message)
                    db.commit();logging.warning('%s: %s',job['kind'],message)
                self.stop.wait(2)
        finally:db.close()
    def run(self,seconds=0):
        db=connect(self.path);thread=threading.Thread(target=self.worker,daemon=True);thread.start();start=time.monotonic()
        try:
            while not self.stop.is_set() and (not seconds or time.monotonic()-start<seconds):
                if not thread.is_alive():raise RuntimeError('HTTP worker stopped')
                running=preferences.active(db)
                muted=meta(db,'notifications_paused','0')=='1' or not running
                ready=not muted and not self.dry_run and load()
                setmeta(db,'notifications_mode','paused' if muted else 'enabled' if ready else 'disabled' if self.dry_run else 'waiting for local setup')
                setmeta(db,'city_state','cooldown' if float(meta(db,'source_cooldown','0'))>time.time() else 'running')
                setmeta(db,'city_heartbeat',time.time());setmeta(db,'monitor_mode','api')
                bucket=int(time.time()//900)*900
                if not db.execute('SELECT 1 FROM metric_history WHERE at=?',(bucket,)).fetchone():
                    totals=coverage(db)
                    db.execute('INSERT INTO metric_history VALUES(?,?,?,?)',(bucket,totals['movies'],totals['venues'],totals['sessions']))
                db.commit()
                if ready:
                    # Serialize sending with preference saves: after a pause is saved,
                    # no new delivery can begin using an earlier settings read.
                    db.execute('BEGIN IMMEDIATE')
                    running=preferences.active(db)
                    if running and meta(db,'notifications_paused','0')!='1':
                        preferences.prime_availability(db)
                        delivered_id=drain(db,commit=False)
                        if delivered_id:preferences.delivered(db,delivered_id)
                    db.commit()
                self.stop.wait(1)
        finally:
            self.stop.set();thread.join(timeout=30);setmeta(db,'city_state','stopped');db.commit();db.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=os.environ.get('BMS_DB','runtime/api.sqlite3'))
    parser.add_argument('--seed-db');parser.add_argument('--seconds',type=int,default=0);parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args();Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    lock=open(args.db+'.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('API monitor already running')
    db=connect(args.db);seed(db,args.seed_db);db.close()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    monitor=Monitor(args.db,args.dry_run)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:monitor.stop.set())
    monitor.run(args.seconds)

if __name__=='__main__':main()
