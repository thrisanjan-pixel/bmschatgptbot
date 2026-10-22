"""One paced HTTP worker, durable state and Telegram delivery. No browser processes."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from api_source import CATALOG,ReadError,end_day,get_json,movies,parse,show_url,today
from local_credentials import load
from store import connect,coverage,emit,enqueue,meta,setmeta
from telegram import drain,send

def schedule(db,event,day,title,notify=False):
    url=show_url(event,day)
    if db.execute('SELECT 1 FROM jobs WHERE id=?',('discovery:'+url,)).fetchone():return
    enqueue(db,'discovery',url,{'event':event,'date':day,'movie':title,'notify_first':notify},end_day(day) if day else 0)

def ingest(db,job,data):
    args=json.loads(job['payload']);result=parse(data,args['event'],args['date'])
    key='api:'+args['event']+':'+result['day'];row=db.execute('SELECT payload FROM snapshots WHERE id=?',(key,)).fetchone()
    previous=json.loads(row[0]) if row else None;current=result['shows']
    title=result['film'] or args['movie']
    for show in current.values():show['movie']=show['movie'] or title
    changes=[]
    if previous is not None or args['notify_first']:
        old=previous or {}
        for identity,show in current.items():
            if identity not in old:
                changes.append('New show · '+show['venue']+' · '+show['time'])
            else:
                before=old[identity].get('categories',{})
                opened=[c['name'] for k,c in show['categories'].items()
                    if k in before and before[k]['status']=='0' and c['status'] in ('1','2','3')]
                if opened:changes.append('Category now available · '+show['venue']+' · '+show['time']+' · '+', '.join(opened))
    if previous!=current:
        revision=int(meta(db,'revision:'+key,'0'))+1;setmeta(db,'revision:'+key,revision)
        if changes:
            first=next(iter(current.values()))
            message=f"🎬 {title}\n{first['language']}\n📅 {result['day']}\n\n"+'\n'.join(changes)
            emit(db,key+':'+str(revision),message[:3300]+'\n\n'+first['url'])
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

class Monitor:
    def __init__(self,path,dry_run=False):self.path=path;self.dry_run=dry_run;self.stop=threading.Event()
    def worker(self):
        db=connect(self.path)
        try:
            while not self.stop.is_set():
                cooldown=float(meta(db,'source_cooldown','0'))
                if cooldown>time.time():self.stop.wait(min(1,cooldown-time.time()));continue
                now=time.time()
                job=db.execute('SELECT * FROM jobs WHERE due<=? AND (expires=0 OR expires>?) ORDER BY (checked=0) DESC,due LIMIT 1',(now,now)).fetchone()
                if not job:self.stop.wait(1);continue
                try:
                    data=get_json(job['url'])
                    if job['kind']=='catalog':
                        listings=movies(data);initialized=bool(meta(db,'catalog_initialized'))
                        for movie in listings:
                            db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?,?)',(movie['url'],movie['title'],now))
                            setmeta(db,'poster:'+movie['url'],movie['image'])
                            schedule(db,movie['event'],'',movie['title'],initialized)
                        setmeta(db,'catalog_initialized','1');setmeta(db,'catalog_last_success',now)
                    else:ingest(db,job,data)
                    db.execute("UPDATE jobs SET checked=?,due=?,error='' WHERE id=?",(time.time(),time.time()+(600 if job['kind']=='catalog' else 120),job['id']))
                    db.commit();logging.info('%s checked',job['kind'])
                except Exception as error:
                    db.rollback();delay=max(300,getattr(error,'retry',300))
                    message=str(error) if isinstance(error,ReadError) else type(error).__name__
                    db.execute('UPDATE jobs SET due=?,error=? WHERE id=?',(time.time()+delay,message,job['id']))
                    if getattr(error,'status',0) in (403,429) or getattr(error,'status',0)>=500:
                        setmeta(db,'source_cooldown',time.time()+delay)
                    db.commit();logging.warning('%s: %s',job['kind'],message)
                self.stop.wait(2)
        finally:db.close()
    def run(self,seconds=0):
        db=connect(self.path);thread=threading.Thread(target=self.worker,daemon=True);thread.start();start=time.monotonic()
        try:
            while not self.stop.is_set() and (not seconds or time.monotonic()-start<seconds):
                if not thread.is_alive():raise RuntimeError('HTTP worker stopped')
                ready=not self.dry_run and load()
                setmeta(db,'notifications_mode','enabled' if ready else 'disabled' if self.dry_run else 'waiting for local setup')
                setmeta(db,'city_state','cooldown' if float(meta(db,'source_cooldown','0'))>time.time() else 'running')
                setmeta(db,'city_heartbeat',time.time());setmeta(db,'monitor_mode','api');db.commit()
                if ready:drain(db)
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
