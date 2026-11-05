"""One paced HTTP worker, durable state and Telegram delivery. No browser processes."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import signal
import threading
import time
from api_source import CATALOG,ReadError,end_day,get_json,movies,no_regional_shows,parse,seat_layout_url,show_url,today
from http_source import SourceWait
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
    upcoming = meta(db, 'upcoming_event:'+args['event']) == '1'
    if (previous is not None or args.get('notify_first') or meta(db,'waiting_show:'+job['id'])=='1') and meta(db,'notifications_paused','0')!='1':
        old=previous or {}
        for rule in preferences.active(db):
            prefs=rule['preferences']
            if prefs.get('row_show'):continue
            groups={}
            for identity,show in current.items():
                if not preferences.matches(show,prefs) and not preferences.matches({**show,'movie':args['movie']},prefs):continue
                open_categories = {
                    code for code, category in show['categories'].items()
                    if category['status'] in ('1','2','3')
                    and preferences.category_matches(category['name'],prefs)
                }
                before = old.get(identity, {}).get('categories', {})
                was_bookable = any(
                    category['status'] in ('1','2','3')
                    and preferences.category_matches(category['name'],prefs)
                    for category in before.values()
                )
                change = None
                if prefs['new_shows'] and ((upcoming and open_categories and not was_bookable) or (not upcoming and identity not in old)):
                    change = ('Booking open · ' if upcoming else 'New show · ') + show['time']
                elif identity in old and prefs['availability']:
                    opened = [preferences.with_price(show,category['name']) for code, category in show['categories'].items()
                              if code in open_categories and code in before and before[code]['status']=='0']
                    if opened:
                        change = 'Category now available · ' + show['time'] + ' · ' + ', '.join(opened)
                if change:
                    group=groups.setdefault(show['venue_code'],{'show':show,'lines':[]})
                    group['lines'].append((change,seat_layout_url(show)))
            if groups:changes[rule['id']]=(rule,groups)
    deferred=False
    if previous!=current:
        revision=int(meta(db,'revision:'+key,'0'))+1
        if changes:
            date=dt.datetime.strptime(result['day'],'%Y%m%d').strftime('%d %b %Y')
            for alert_id,(rule,groups) in changes.items():
                prefs=rule['preferences'];messages=[]
                for venue,group in groups.items():
                    show=group['show']
                    heading=f"🎬 {title}\n{show['language']}\n📅 {date}\n📍 {show['venue']}\n\n"
                    chunks=['']
                    for line,url in group['lines']:
                        block=line+'\n'+url+'\n'
                        if len(chunks[-1])+len(block)>2800:chunks.append('')
                        chunks[-1]+=block
                    for index,chunk in enumerate(chunks):
                        text=heading+chunk;messages.append((f'{alert_id}:{key}:{revision}:{venue}:{index}:'+hashlib.sha1(text.encode()).hexdigest()[:8],text))
                # Already queued by an earlier deferred pass of this same baseline.
                if all(db.execute('SELECT 1 FROM outbox WHERE id=?',(hashlib.sha256(event.encode()).hexdigest(),)).fetchone() for event,_ in messages):continue
                now=time.time()
                pending=db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND alert_id=? LIMIT 1',(alert_id,)).fetchone()
                # The interval spaces separate ingests; one ingest's changes go out together.
                if now-rule['last_alert']>=prefs['interval_minutes']*60 and (not pending or prefs['interval_minutes']==0):
                    for event,text in messages:emit(db,event,text,alert_id=alert_id)
                    db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,alert_id))
                    rule['last_alert']=now
                else:
                    # Keep the old baseline so the unsent changes are found again next poll.
                    deferred=True;setmeta(db,'watch_skipped',int(meta(db,'watch_skipped','0'))+1)
        if not deferred:setmeta(db,'revision:'+key,revision)
    if not deferred:db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)',(key,json.dumps(current)))
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
    db.execute('DELETE FROM meta WHERE key=?',('waiting_show:'+job['id'],))
    setmeta(db,'api_last_success',time.time())

def seed(db,previous_db=None):
    enqueue(db,'catalog',CATALOG,{})
    register_movie(db,'https://in.bookmyshow.com/movies/hyderabad/ene-repeat/ET00506990','ENE Repeat')
    setmeta(db,'upcoming_event:ET00506990','1')
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
    if not is_watched(json.loads(job['payload']),wanted_dates(db)):
        return 120
    # Keep a conservative target until real source access is stable.
    return 60


def watched_movies(db):
    return sorted({preferences.normalized(m) for rule in preferences.active(db)
                   for m in rule['preferences']['movies'] if not rule['preferences']['row_show']})


BACKGROUND_SPACING=180


def wanted_dates(db):
    """What showtime alerts need: per movie, each alert's dates (None = any date) and
    its language, format and screen filters, plus what each known version has shown."""
    movies={}
    for rule in preferences.active(db):
        p=rule['preferences']
        if p['row_show']:continue
        probe={**preferences.defaults(),**{k:p[k] for k in ('languages','formats','screens')}}
        for movie in p['movies']:
            movies.setdefault(preferences.normalized(movie),[]).append((set(p['dates']) or None,probe))
    profiles={}
    for event,language,screen in db.execute("""SELECT DISTINCT json_extract(payload,'$.event'),
            json_extract(payload,'$.language'),json_extract(payload,'$.format') FROM sessions"""):
        profiles.setdefault(event,[]).append({'movie':'','venue_code':'','date':'','categories':{},
            'language':language or '','format':screen or ''})
    return {'movies':movies,'profiles':profiles}


def is_watched(payload,wanted):
    # Background work cannot crowd out the listings alerts depend on. A listing is
    # background when no alert asks for its date, or when its version (event) has
    # only ever shown in languages, formats or screens no alert asks for. Versions
    # with no showtimes seen yet, and undated listings, still count.
    entries=wanted['movies'].get(preferences.normalized(payload.get('movie','')))
    if not entries:return False
    day=payload.get('date','');shows=wanted['profiles'].get(payload.get('event'))
    return any((dates is None or not day or day in dates) and
               (not shows or any(preferences.matches(show,probe) for show in shows))
               for dates,probe in entries)


def next_job(db, now):
    wanted = wanted_dates(db)
    jobs = [(r['id'],json.loads(r['payload']),r['checked']) for r in db.execute('SELECT id,payload,checked FROM jobs')]
    movies = [job_id for job_id,payload,_ in jobs if is_watched(payload,wanted)]
    # Movies take turns: the alert movie checked longest ago goes next, so a movie
    # with many date and format listings cannot crowd out one with a single listing.
    last_checked = {}
    for job_id,payload,checked in jobs:
        key = preferences.normalized(payload.get('movie',''))
        last_checked[key] = max(last_checked.get(key,0),checked or 0)
    slots = ','.join('?' for _ in movies) or "''"
    cooldown_until = float(meta(db, 'source_cooldown', '0'))
    recovering = bool(cooldown_until and 0 <= now - cooldown_until < 60)
    # Listing requests are one a minute, so a 60 s window would hand background
    # work every turn. Give it at most one turn in three.
    background_recent = now-float(meta(db,'listing_background_attempt','0')) < BACKGROUND_SPACING
    background_paused = float(meta(db, 'background_cooldown_until', '0')) > now
    rows = db.execute(f"""WITH eligible AS (
        SELECT *, id IN ({slots}) AS watched
        FROM jobs WHERE due<=? AND (expires=0 OR expires>?)
      )
      SELECT *, CASE WHEN NOT ? AND due>0 AND due<? THEN 0
        WHEN watched THEN 1 ELSE 2 END AS rank
      FROM eligible WHERE watched OR (NOT ? AND NOT ?)""",
      (*movies, now, now, recovering or background_recent, now-300, background_paused, background_recent)).fetchall()
    def order(row):
        turn = last_checked.get(preferences.normalized(json.loads(row['payload']).get('movie','')),0) if row['watched'] else -1
        return (row['rank'], turn, row['due'], row['checked'])
    return min(rows, key=order, default=None)


def record_listing_failure(db, job, error, started, now):
    status = error.status if isinstance(error, ReadError) else 0
    delay = error.retry if isinstance(error, ReadError) else 300
    message = str(error) if isinstance(error, ReadError) else type(error).__name__
    if getattr(error,'diagnostic',None):
        setmeta(db,'listing_last_refusal',json.dumps({**error.diagnostic,'at':now}))
    if status == 403 and job['error'] == 'Source HTTP 403':
        delay = max(300, delay)

    setmeta(db, 'listing_success_streak:'+job['id'], 0)
    db.execute('UPDATE jobs SET due=?,error=? WHERE id=?', (now+delay, message, job['id']))
    record_check(db, 'listings', job['id'], started, message, now=now)

    if status == 403:
        active_movies = set(watched_movies(db))
        refused_jobs = db.execute(
            """SELECT jobs.payload FROM check_stats
               JOIN jobs ON jobs.id=check_stats.job_key
               WHERE source='listings' AND check_stats.error='Source HTTP 403'
                 AND last_attempt>=? AND jobs.kind=?""",
            (now-60, job['kind']),
        )
        background_refusals = 0
        for row in refused_jobs:
            movie = preferences.normalized(json.loads(row['payload']).get('movie',''))
            if movie not in active_movies:
                background_refusals += 1
        if background_refusals >= 3:
            setmeta(db, 'background_cooldown_until', now+max(300, delay))

    if status == 429:
        delay = max(300, delay)
        setmeta(db, 'source_cooldown', now+delay)
        setmeta(db, 'source_cooldown_started', now)
        setmeta(db, 'source_cooldown_duration', delay)
    return message


class Monitor:
    def __init__(self,path,dry_run=False):self.path=path;self.dry_run=dry_run;self.stop=threading.Event()
    def worker(self):
        db=connect(self.path)
        try:
            while not self.stop.is_set():
                try:self.check(db)
                except Exception as error:
                    # A locked database must not end the only listing worker; retry shortly.
                    try:db.rollback()
                    except Exception:pass
                    logging.warning('listing worker: %s; retrying',type(error).__name__);self.stop.wait(5)
        finally:db.close()
    def check(self,db):
        cooldown=float(meta(db,'source_cooldown','0'))
        if cooldown>time.time():self.stop.wait(min(1,cooldown-time.time()));return
        # Pick a job only once the request budget is free, so waiting never reshuffles the queue.
        budget=float(meta(db,'listing_budget_until','0'))
        if budget>time.time():self.stop.wait(min(1,budget-time.time()));return
        now=time.time()
        job=next_job(db,now)
        if not job:self.stop.wait(1);return
        try:
            started=time.time()
            setmeta(db,'listing_check_started',started)
            if not is_watched(json.loads(job['payload']),wanted_dates(db)):
                setmeta(db,'listing_background_attempt',started)
            db.commit()
            data=get_json(job['url'])
            if job['kind']=='catalog':
                listings=movies(data);initialized=bool(meta(db,'catalog_initialized'))
                for movie in listings:
                    db.execute('INSERT OR REPLACE INTO inventory VALUES(?,?,?)',(movie['url'],movie['title'],now))
                    setmeta(db,'poster:'+movie['url'],movie['image'])
                    schedule(db,movie['event'],'',movie['title'],initialized)
                setmeta(db,'catalog_initialized','1');setmeta(db,'catalog_last_success',now)
            elif no_regional_shows(data):
                # Keep prior show state and wait for a verified showtime payload.
                setmeta(db,'waiting_show:'+job['id'],'1')
                setmeta(db,'upcoming_event:'+json.loads(job['payload'])['event'],'1')
                setmeta(db,'api_last_success',time.time())
            else:ingest(db,job,data)
            streak_key='listing_success_streak:'+job['id']
            setmeta(db,streak_key,min(5,int(meta(db,streak_key,'0'))+1))
            db.execute("UPDATE jobs SET checked=?,due=?,error='' WHERE id=?",(time.time(),time.time()+poll_interval(db,job),job['id']))
            record_check(db,'listings',job['id'],started)
            db.commit();logging.info('%s checked',job['kind'])
        except Exception as error:
            db.rollback()
            if isinstance(error,SourceWait):
                # Leave the job's place in the queue alone; only the budget waits.
                setmeta(db,'listing_budget_until',error.until);db.commit()
            else:
                message=record_listing_failure(db,job,error,started,time.time())
                db.commit();logging.warning('%s: %s',job['kind'],message)
        finally:
            setmeta(db,'listing_check_started','0');db.commit()
        self.stop.wait(2)
    def run(self,seconds=0):
        db=connect(self.path);thread=threading.Thread(target=self.worker,daemon=True);thread.start();start=time.monotonic()
        try:
            while not self.stop.is_set() and (not seconds or time.monotonic()-start<seconds):
                if not thread.is_alive():raise RuntimeError('HTTP worker stopped')
                try:self.tick(db)
                except Exception as error:
                    # Keep heartbeats and delivery alive through a locked database or bad stored row.
                    try:db.rollback()
                    except Exception:pass
                    logging.warning('monitor loop: %s; retrying',type(error).__name__);self.stop.wait(5)
        finally:
            self.stop.set();thread.join(timeout=30);setmeta(db,'city_state','stopped');db.commit();db.close()
    def tick(self,db):
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
                # A bad stored showtime must not hold back delivery of queued alerts.
                db.execute('SAVEPOINT prime')
                try:preferences.prime_availability(db)
                except Exception as error:
                    db.execute('ROLLBACK TO prime');logging.warning('initial availability check: %s',type(error).__name__)
                db.execute('RELEASE prime')
                delivered_id=drain(db,commit=False)
                if delivered_id:preferences.delivered(db,delivered_id)
            db.commit()
        self.stop.wait(1)

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
