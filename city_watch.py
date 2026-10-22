"""Automatic Hyderabad-wide polling with durable state and delivery queue."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time

from parsers import (Catalog, cinema_links, day_end, format_seats, format_shows,
                     movie_pages, offered_dates, parse_venue, show_data, today)
from source import Browser, CATALOG, SourceError
from store import connect, coverage, emit, enqueue, meta, setmeta, snapshot
from telegram import drain

DEFAULT_DB = '/workspace/booking-tracker/state.sqlite3'
KINDS = ('catalog', 'movie', 'discovery', 'venue', 'seat')
INTERVAL = {'catalog':300, 'movie':300, 'discovery':300, 'venue':30, 'seat':30}


def add_venue(db, venue, day=None):
    db.execute('INSERT INTO venues(code,name,url,initialized) VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE SET url=excluded.url',
               (venue['code'], venue['name'], venue['url'],int(meta(db,'city_initialized','0'))))
    day = day or today()
    enqueue(db, 'venue', venue['url']+'/'+day,
            {'code':venue['code'], 'date':day, 'base':venue['url']}, day_end(day))


def ingest_venue(db, job, data):
    p = json.loads(job['payload'])
    shows = parse_venue(data, p['code'], p['date'], job['url'])
    initialized = db.execute('SELECT initialized FROM venues WHERE code=?', (p['code'],)).fetchone()[0]
    existing = db.execute('SELECT payload FROM snapshots WHERE id=?', ('shows:'+job['url'],)).fetchone()
    previous = json.loads(existing[0]) if existing else {}
    current = {s['id']: s for s in shows}
    new = [s for s in shows if s['id'] not in previous]
    if initialized and new:
        emit(db, 'shows:'+':'.join(sorted(s['id'] for s in new)), format_shows(new))
    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)', ('shows:'+job['url'], json.dumps(current)))
    for old in db.execute('SELECT id,payload FROM sessions WHERE active=1').fetchall():
        show = json.loads(old['payload'])
        if show['venue_code']==p['code'] and show['date']==p['date'] and old['id'] not in current:
            db.execute('UPDATE sessions SET active=0 WHERE id=?', (old['id'],))
            db.execute('UPDATE jobs SET expires=1 WHERE id=?', ('seat:'+show['seat_url'],))
    for s in shows:
        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,1)', (s['id'], json.dumps(s), s['cutoff']))
        enqueue(db,'seat',s['seat_url'],s,s['cutoff'])
        if not s['available']:
            # Coarse unavailable data may seed an empty baseline, never erase a real map.
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?)', ('seats:'+s['id'], '{}'))
    for day in offered_dates(data):
        enqueue(db,'venue',p['base']+'/'+day,dict(p,date=day),day_end(day))
    return shows


def seed(db):
    enqueue(db, 'catalog', CATALOG, {})
    # Preserve the verified inventory retained by the earlier application.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='movies'").fetchone():
        for row in db.execute('SELECT url,title FROM movies').fetchall():
            db.execute('INSERT OR IGNORE INTO inventory VALUES(?,?,?)', (row['url'],row['title'],time.time()))
    for movie in db.execute('SELECT * FROM inventory WHERE seen>?', (time.time()-86400,)).fetchall():
        enqueue(db,'movie',movie['url'],{'title':movie['title']},time.time()+86400)
    for venue in db.execute('SELECT * FROM venues').fetchall():
        add_venue(db,dict(venue))
    db.commit()


class Runtime:
    def __init__(self, db_path, dry_run=False):
        self.db_path = db_path
        self.dry_run = dry_run
        self.stop = threading.Event()
        self.rate = threading.Lock()
        self.last_request = 0
        db = connect(db_path)
        self.cooldown = float(meta(db,'source_cooldown','0'))
        seed(db)
        db.close()

    def throttle(self):
        with self.rate:
            while True:
                delay = max(self.cooldown-time.time(), 1-(time.monotonic()-self.last_request), 0)
                if delay<=0:break
                if self.stop.wait(min(delay,1)):return False
            self.last_request = time.monotonic()
            return True

    def process(self, db, browser, job):
        kind, url = job['kind'], job['url']
        p = json.loads(job['payload'])
        if kind=='seat':
            row = db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',
                             (p['id'],time.time())).fetchone()
            if not row:
                db.execute('UPDATE jobs SET expires=1 WHERE id=?',(job['id'],))
                return
            show = json.loads(row[0])
            if not show['available']:
                return
            from seats import read_seats
            available, photo = read_seats(browser,show)
            if time.time() < show['cutoff']:
                snapshot(db,'seats:'+show['id'],available,
                         lambda added:format_seats(show,available,added),photo)
            return
        document = browser.read(url, catalog=kind=='catalog')
        if kind=='catalog':
            parser = Catalog()
            parser.feed(document)
            if not parser.movies:
                raise SourceError('Empty catalogue; previous data retained')
            for movie_url,title in parser.movies.items():
                db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?,?)', (movie_url,title,time.time()))
                enqueue(db,'movie',movie_url,{'title':title},time.time()+86400)
            setmeta(db,'catalog_last_success',time.time())
        elif kind=='movie':
            for item in movie_pages(url,document):
                enqueue(db,'discovery',item['url'],item,day_end(item['date']))
        elif kind=='discovery':
            data = show_data(document,kind,p['event'],p['date'])
            for venue in cinema_links(data):
                add_venue(db,venue,p['date'])
            base = url.rsplit('/',1)[0]
            for day in offered_dates(data):
                enqueue(db,'discovery',base+'/'+day,dict(p,date=day,url=base+'/'+day),day_end(day))
        elif kind=='venue':
            ingest_venue(db,job,show_data(document,kind,p['code'],p['date']))

    def worker(self,kind):
        db = connect(self.db_path)
        browser = None
        try:
            while not self.stop.is_set():
                now = time.time()
                job = db.execute('''SELECT * FROM jobs WHERE kind=? AND due<=?
                    AND (expires=0 OR expires>?) ORDER BY due,id LIMIT 1''',(kind,now,now)).fetchone()
                if not job:
                    self.stop.wait(1)
                    continue
                if not self.throttle():
                    break
                try:
                    if browser is None:
                        browser = Browser()
                    self.process(db,browser,job)
                    checked = time.time()
                    db.execute('UPDATE jobs SET checked=?,due=?,error=\'\' WHERE id=?',
                               (checked,checked+INTERVAL[kind],job['id']))
                    if kind=='venue':
                        code=json.loads(job['payload'])['code']
                        pending=db.execute('''SELECT count(*) FROM jobs WHERE kind='venue' AND checked=0
                            AND expires>? AND json_extract(payload,'$.code')=?''',(checked,code)).fetchone()[0]
                        if not pending:
                            db.execute('UPDATE venues SET initialized=1 WHERE code=?',(code,))
                    db.commit()
                    logging.info('%s checked',kind)
                except Exception as error:
                    db.rollback()
                    status=getattr(error,'status',0)
                    delay=max(300,getattr(error,'retry',300))
                    message=str(error) if isinstance(error,SourceError) else type(error).__name__
                    db.execute('UPDATE jobs SET due=?,error=? WHERE id=?',(time.time()+delay,message,job['id']))
                    if status in (403,429) or status>=500:
                        self.cooldown=max(self.cooldown,time.time()+delay)
                        setmeta(db,'source_cooldown',self.cooldown)
                    db.commit()
                    logging.warning('%s: %s; retry in %ss',kind,message,delay)
                    # Browser crashes should recover on the next attempt.
                    if type(error).__name__ in ('TargetClosedError','Error') and browser:
                        try: browser.close()
                        except Exception: pass
                        browser=None
        finally:
            if browser:
                try: browser.close()
                except Exception: pass
            db.close()

    def run(self,seconds=0):
        db=connect(self.db_path)
        threads=[threading.Thread(target=self.worker,args=(k,),name=k,daemon=True) for k in KINDS]
        for thread in threads:thread.start()
        start=time.monotonic()
        last_seed=0
        try:
            while not self.stop.is_set() and (not seconds or time.monotonic()-start<seconds):
                if not all(t.is_alive() for t in threads):
                    raise RuntimeError('Monitoring worker stopped unexpectedly')
                if time.monotonic()-last_seed>60:
                    seed(db);last_seed=time.monotonic()
                pending=db.execute('''SELECT count(*) FROM jobs WHERE kind IN ('catalog','movie','discovery','venue')
                    AND checked=0 AND (expires=0 OR expires>?)''',(time.time(),)).fetchone()[0]
                if not pending and db.execute('SELECT count(*) FROM venues').fetchone()[0]:
                    setmeta(db,'city_initialized','1')
                setmeta(db,'city_heartbeat',time.time())
                setmeta(db,'city_state','cooldown' if self.cooldown>time.time() else 'running')
                db.commit()
                if not self.dry_run:drain(db)
                self.stop.wait(1)
        finally:
            self.stop.set()
            for thread in threads:thread.join(timeout=50)
            setmeta(db,'city_state','stopped');db.commit();db.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=os.environ.get('BMS_DB',DEFAULT_DB))
    parser.add_argument('--seconds',type=int,default=0)
    parser.add_argument('--dry-run',action='store_true',help='Suppress Telegram; still writes the selected database')
    args=parser.parse_args()
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    lock=open(args.db+'.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A monitor already owns this database')
    if not args.dry_run and not all(os.environ.get(k) for k in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')):
        raise SystemExit('Configure Telegram credentials securely before starting')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    runtime=Runtime(args.db,args.dry_run)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:runtime.stop.set())
    runtime.run(args.seconds)


if __name__=='__main__':main()
