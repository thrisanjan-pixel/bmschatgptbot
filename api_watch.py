"""One paced HTTP worker, durable state and Telegram delivery. No browser processes."""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import signal
import threading
import time
from api_source import CATALOG,ReadError,WrongDateError,end_day,get_json,movies,no_regional_shows,parse,seat_layout_url,show_url,today
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
    preferences.drop_unbookable_gates(db,current)
    upcoming = meta(db, 'upcoming_event:'+args['event']) == '1'
    if (previous is not None or args.get('notify_first') or meta(db,'waiting_show:'+job['id'])=='1') and meta(db,'notifications_paused','0')!='1':
        for rule in preferences.active(db):
            prefs=rule['preferences']
            if prefs.get('row_show'):continue
            # An alert that already sent this listing's changes while another alert waited
            # compares against its own newer view, so it does not repeat them.
            own=db.execute('SELECT payload FROM snapshots WHERE id=?',(key+'#'+rule['id'],)).fetchone()
            old=json.loads(own[0]) if own else (previous or {})
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
                change = None;names = []
                if prefs['new_shows'] and ((upcoming and open_categories and not was_bookable) or (not upcoming and identity not in old)):
                    change = ('Booking open · ' if upcoming else 'New show · ') + show['time']
                    names = [category['name'] for code, category in show['categories'].items() if code in open_categories]
                elif identity in old and prefs['availability']:
                    # A seat type opens when it was not bookable at the last check: sold out ('0'),
                    # blocked ('4') or not listed yet for this show.
                    names = [category['name'] for code, category in show['categories'].items()
                             if code in open_categories and before.get(code,{}).get('status') not in ('1','2','3')]
                    opened = [preferences.with_price(show,name) for name in names]
                    if opened:
                        change = 'Category now available · ' + show['time'] + ' · ' + ', '.join(opened)
                if change:
                    group=groups.setdefault(show['venue_code'],{'show':show,'lines':[]})
                    group['lines'].append((change,seat_layout_url(show),show,names))
            if groups:changes[rule['id']]=(rule,groups)
    deferred=False;waiting=set()
    if previous!=current:
        revision=int(meta(db,'revision:'+key,'0'))+1
        if changes:
            date=dt.datetime.strptime(result['day'],'%Y%m%d').strftime('%d %b %Y')
            heading_for=lambda show:f"🎬 {title}\n{show['language']}\n📅 {date}\n📍 {show['venue']}\n\n"
            for alert_id,(rule,groups) in changes.items():
                prefs=rule['preferences'];messages=[]
                for venue,group in groups.items():
                    show=group['show']
                    heading=heading_for(show)
                    chunks=['']
                    for line,url,_,_ in group['lines']:
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
                    rows = preferences.rows_enabled();minimum = prefs.get('min_seats',0)
                    if minimum and rows:
                        # Hold the alert until a seat-map read shows enough open seats (row_watch.process_seat_photos sends it).
                        for group in groups.values():
                            for line,url,show,names in group['lines']:
                                if names:preferences.queue_seat_photo(db,alert_id,show['id'],names,'gate',heading_for(show)+line+'\n'+url+'\n',now)
                    else:
                        for event,text in messages:emit(db,event,text+('\n(seat count unavailable)' if minimum else ''),alert_id=alert_id)
                        if rows:
                            # The text goes out at once; the seat-map photo follows after the next seat read.
                            for group in groups.values():
                                for line,url,show,names in group['lines']:preferences.queue_seat_photo(db,alert_id,show['id'],names,'photo','',now)
                        db.execute('UPDATE alerts SET last_alert=? WHERE id=?',(now,alert_id))
                        rule['last_alert']=now
                else:
                    # Keep the old baseline so the unsent changes are found again next poll.
                    deferred=True;waiting.add(alert_id);setmeta(db,'watch_skipped',int(meta(db,'watch_skipped','0'))+1)
        if not deferred:setmeta(db,'revision:'+key,revision)
    if deferred:
        # The shared baseline stays old for the waiting alerts; every other alert moves on.
        for rule in preferences.active(db):
            if rule['id'] not in waiting and not rule['preferences'].get('row_show'):
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)',(key+'#'+rule['id'],json.dumps(current)))
    else:db.execute('DELETE FROM snapshots WHERE id>=? AND id<?',(key+'#',key+'$'))
    # Once any show of the event is bookable the event is no longer "upcoming". A deferred pass
    # keeps the flag so its retry still says "Booking open".
    if upcoming and not deferred and any(c['status'] in ('1','2','3') for show in current.values() for c in show['categories'].values()):
        db.execute('DELETE FROM meta WHERE key=?',('upcoming_event:'+args['event'],))
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
    from http_source import policy,listing_interval
    return policy('in.bookmyshow.com',listing_gap=listing_interval(db))[1]


def watched_movies(db):
    return sorted({preferences.movie_key(m) for rule in preferences.active(db)
                   for m in rule['preferences']['movies'] if rule['preferences']['row_show'] in ('',preferences.ANY_SHOW)})


BACKGROUND_SPACING=300


def wanted_dates(db,urgent=False):
    """What showtime alerts need: per movie, each alert's dates (None = any date) and
    its language, format and screen filters, plus what each known version has shown.
    A '*' row alert needs fresh showtime lists too, to find new showtimes. With urgent=True only urgent alerts count."""
    movies={}
    for rule in preferences.active(db):
        p=rule['preferences']
        if p['row_show'] not in ('',preferences.ANY_SHOW) or (urgent and not p['urgent']):continue
        probe={**preferences.defaults(),**{k:p[k] for k in ('languages','formats') if k in p}}
        if not p['cinemas']:probe['screens']=p['screens']
        for movie in p['movies']:
            movies.setdefault(preferences.movie_key(movie),[]).append((set(p['dates']) or None,probe))
    profiles={}
    for event,language,screen in db.execute("""SELECT DISTINCT json_extract(payload,'$.event'),
            json_extract(payload,'$.language'),json_extract(payload,'$.format') FROM sessions"""):
        profiles.setdefault(event,[]).append({'movie':'','venue_code':'','date':'','categories':{},
            'language':language or '','format':screen or ''})
    # Dated listings that have not expired; an expired one no longer covers its date.
    dated={(p.get('event'),p.get('date')) for p in (json.loads(r[0]) for r in db.execute("SELECT payload FROM jobs WHERE kind='discovery' AND (expires=0 OR expires>?)",(time.time(),))) if p.get('date')}
    return {'movies':movies,'profiles':profiles,'dated':dated}


def is_watched(payload,wanted):
    # Background work cannot crowd out the listings alerts depend on. A listing is
    # background when no alert asks for its date, or when its version (event) has
    # only ever shown in languages, formats or screens no alert asks for. Versions
    # with no showtimes seen yet, and undated listings, still count.
    entries=wanted['movies'].get(preferences.movie_key(payload.get('movie','')))
    if not entries:return False
    day=payload.get('date','');event=payload.get('event');shows=wanted['profiles'].get(event)
    # An undated listing only finds new dates; once every date an alert wants has
    # its own listing, the undated one is background work for that alert.
    # An alert with no dates is covered too once the event has a live dated listing: every
    # dated response schedules newly appearing dates and variants (see ingest).
    dated=wanted.get('dated',())
    covered=lambda dates:not day and (any(e==event and d for e,d in dated) if event and dates is None else dates is not None and all((event,d) in dated for d in dates))
    return any((dates is None or not day or day in dates) and not covered(dates) and
               (not shows or any(preferences.matches(show,probe) for show in shows))
               for dates,probe in entries)


def listing_refresh(db, now, queue=False, inventory=False):
    """Queue a paced refresh or report the latest results from the requested pass."""
    key='inventory_listing_refresh' if inventory else 'manual_listing_refresh'
    if queue and not inventory:
        wanted=wanted_dates(db)
        exact={rule['preferences']['row_show'] for rule in preferences.active(db)}-{'',preferences.ANY_SHOW}
        exact_dates={(show['event'],show['date']) for _,show in preferences.future_shows(db) if show.get('id') in exact} if exact else set()
    if queue:
        db.execute('BEGIN IMMEDIATE')
    batch=json.loads(meta(db,key,'{}'))
    rows=[]
    if batch:
        rows=db.execute("""SELECT target.value AS id,j.expires,
                   MAX(COALESCE(s.last_attempt,0),COALESCE(j.checked,0)) AS last_attempt,
                   CASE WHEN j.checked>=COALESCE(s.last_attempt,0) THEN '' ELSE s.error END AS error
            FROM json_each(?) target LEFT JOIN jobs j ON j.id=target.value
            LEFT JOIN check_stats s ON s.job_key=j.id AND s.source='listings'""",
            (json.dumps(batch['ids']),)).fetchall()
    checked=[r for r in rows if (r['last_attempt'] or 0)>=batch['at']]
    pending=[r['id'] for r in rows if (r['last_attempt'] or 0)<batch['at']
             and r['expires'] is not None and (not r['expires'] or r['expires']>now)]
    if queue and not pending:
        if inventory:
            enqueue(db,'catalog',CATALOG,{})
            pending=inventory_jobs(db,now)
        else:
            pending=[r['id'] for r in db.execute("""SELECT id,payload FROM jobs
                     WHERE kind='discovery' AND (expires=0 OR expires>?)""",(now,))
                     if is_watched((payload:=json.loads(r['payload'])),wanted)
                     or (payload.get('event'),payload.get('date')) in exact_dates]
        batch={'at':now,'ids':pending}
        setmeta(db,key,json.dumps(batch))
        db.executemany("UPDATE jobs SET due=MIN(due,?) WHERE id=? AND error=''",
                       [(now,job_id) for job_id in pending])
        checked=[]
    if queue:db.commit()
    return {'total':len(batch.get('ids',[])),'pending':pending,'checked':sum(not r['error'] for r in checked),
            'failed':sum(bool(r['error']) for r in checked)}


def set_listing_interval(db, seconds):
    from http_source import LISTING_INTERVALS,listing_interval
    if type(seconds) is not int or seconds not in LISTING_INTERVALS:
        raise ValueError('Choose 30, 60 or 120 seconds')
    db.execute('BEGIN IMMEDIATE')
    previous=listing_interval(db)
    now=time.time()
    if previous!=seconds:
        wanted=wanted_dates(db)
        for row in db.execute("SELECT id,payload,checked,due FROM jobs WHERE kind='discovery' AND checked>0 AND due>? AND error=''",(now,)).fetchall():
            if is_watched(json.loads(row['payload']),wanted) and row['due']-row['checked']<=previous*2+1:
                db.execute('UPDATE jobs SET due=? WHERE id=?',(max(now,row['checked']+seconds),row['id']))
        setmeta(db,'listing_interval',seconds)
    db.commit()


def inventory_jobs(db,now):
    return [r[0] for r in db.execute("""SELECT id FROM jobs WHERE kind IN ('catalog','discovery')
                                       AND (expires=0 OR expires>?) ORDER BY id""",(now,))]


def extend_inventory_refresh(db, job_id, now):
    """Include movies, versions and dates discovered during the current refresh."""
    if job_id not in listing_refresh(db,now,inventory=True)['pending']:return
    batch=json.loads(meta(db,'inventory_listing_refresh'))
    known=set(batch['ids'])
    new=[job for job in inventory_jobs(db,now) if job not in known]
    if new:
        batch['ids'].extend(new)
        setmeta(db,'inventory_listing_refresh',json.dumps(batch))


def next_job(db, now):
    rows = listing_order(db, now)
    return rows[0] if rows else None


def listing_order(db, now):
    """Due listing jobs in the order the worker will take them."""
    wanted = wanted_dates(db)
    jobs = [(r['id'],json.loads(r['payload'])) for r in db.execute('SELECT id,payload FROM jobs')]
    movies = [i for i,payload in jobs if is_watched(payload,wanted)];watched_ids = set(movies)
    # Listings an urgent alert watches count their overdue time three times.
    urgent_wanted = wanted_dates(db,True)
    hot = {i for i,payload in jobs if i in watched_ids and is_watched(payload,urgent_wanted)} if urgent_wanted['movies'] else set()
    slots = ','.join('?' for _ in movies) or "''"
    cooldown_until = float(meta(db, 'source_cooldown', '0'))
    recovering = bool(cooldown_until and 0 <= now - cooldown_until < 60)
    # Listing requests are one a minute, so a 60 s window would hand background
    # work every turn. Give it at most one turn in five.
    background_recent = now-float(meta(db,'listing_background_attempt','0')) < BACKGROUND_SPACING
    # BMS_BACKGROUND_LISTINGS=0 gives every listing request to alert listings while any alert
    # watches one; movies no alert asks for wait until no alert listing is due.
    inventory = listing_refresh(db, now, inventory=True)['pending']
    inventory_cooling = float(meta(db, 'background_cooldown_until', '0')) > now
    background_paused = inventory_cooling or (
        os.environ.get('BMS_BACKGROUND_LISTINGS','1')=='0' and bool(movies))
    rows = db.execute(f"""WITH eligible AS (
        SELECT *, id IN ({slots}) AS watched, id IN (SELECT value FROM json_each(?)) AS inventory
        FROM jobs WHERE due<=? AND (expires=0 OR expires>?)
      )
      SELECT * FROM eligible WHERE watched OR (inventory AND kind='catalog') OR (inventory AND NOT ? AND NOT ?) OR (NOT ? AND NOT ?)""",
      (*movies, json.dumps(inventory), now, now, inventory_cooling or recovering, background_recent, background_paused, background_recent)).fetchall()
    # A never-checked alert listing joins the end of the overdue line (as if due 300 s ago).
    # Store that place once, so it ages like the others; a place recomputed every turn
    # never got older and lost to routine listings forever.
    fresh = [row['id'] for row in rows if row['watched'] and not row['due']]
    if fresh:
        db.executemany('UPDATE jobs SET due=? WHERE id=? AND due=0', [(now-300, i) for i in fresh]);db.commit()
    pinned = set(fresh)
    def base(row):
        return now-300 if row['id'] in pinned else row['due']
    def effective(row):
        # The urgent boost counts from the first stored place, not the one pinned this turn.
        return now-(now-row['due'])*3 if row['id'] in hot and row['due'] else base(row)
    def order(row):
        # Alert listings take turns by how overdue they are. A never-checked listing
        # joins the end of the overdue line (as if due 300 s ago) instead of jumping ahead of
        # long-overdue ones; an unwatched one keeps waiting its turn.
        due=effective(row)
        rank=0 if not (recovering or background_recent) and (base(row)>0 or row['watched']) and due<now-300 else 1 if row['watched'] else 2
        if row['inventory'] and row['kind']=='catalog':rank=-1
        elif row['inventory'] and not row['watched']:rank=0
        return (rank, due or (now-300 if row['watched'] else now), row['checked'])
    return sorted(rows, key=order)


def record_listing_failure(db, job, error, started, now):
    if isinstance(error, WrongDateError) and error.unlisted:
        # BookMyShow does not list this date for this version: a successful check that found no
        # shows. Its showtimes stop matching alerts, and an empty baseline makes them count as
        # new if BookMyShow lists the date again.
        args=json.loads(job['payload'])
        for old in db.execute('SELECT id,payload FROM sessions WHERE active=1').fetchall():
            show=json.loads(old['payload'])
            if show.get('event')==args.get('event') and show.get('date')==args.get('date'):
                db.execute('UPDATE sessions SET active=0 WHERE id=?',(old['id'],))
        key='api:'+str(args.get('event'))+':'+str(args.get('date'))
        db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)',(key,'{}'))
        db.execute('DELETE FROM snapshots WHERE id>=? AND id<?',(key+'#',key+'$'))
        db.execute("UPDATE jobs SET checked=?,due=?,error='' WHERE id=?",(now,now+error.retry,job['id']))
        record_check(db,'listings',job['id'],started,now=now)
        return 'Date not listed by BookMyShow; no shows'
    if isinstance(error, WrongDateError):
        message='BookMyShow answered for another event or an invalid date'
        db.execute('UPDATE jobs SET due=?,error=? WHERE id=?',(now+error.retry,message,job['id']))
        record_check(db,'listings',job['id'],started,message,now=now)
        return message

    status = error.status if isinstance(error, ReadError) else 0
    # A transport error or status-0 read retries after the 60 s request gap the budget already enforces.
    delay = error.retry if isinstance(error, ReadError) and error.status else 60
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
            movie = preferences.movie_key(json.loads(row['payload']).get('movie',''))
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
        next_heartbeat=0
        try:
            while not self.stop.is_set():
                try:
                    monotonic_now=time.monotonic()
                    if monotonic_now>=next_heartbeat:
                        setmeta(db,'listing_worker_heartbeat',time.time())
                        db.commit()
                        next_heartbeat=monotonic_now+5
                    self.check(db)
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
            stamped=not is_watched(json.loads(job['payload']),wanted_dates(db))
            if stamped:
                previous_attempt=meta(db,'listing_background_attempt','0')
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
                # Only the undated listing says the event has no shows yet; a dated one may just be an empty day.
                if not json.loads(job['payload']).get('date'):setmeta(db,'upcoming_event:'+json.loads(job['payload'])['event'],'1')
                setmeta(db,'api_last_success',time.time())
            else:ingest(db,job,data)
            extend_inventory_refresh(db,job['id'],time.time())
            streak_key='listing_success_streak:'+job['id']
            setmeta(db,streak_key,min(5,int(meta(db,streak_key,'0'))+1))
            db.execute("UPDATE jobs SET checked=?,due=?,error='' WHERE id=?",(time.time(),time.time()+poll_interval(db,job),job['id']))
            record_check(db,'listings',job['id'],started)
            db.commit();logging.info('%s checked',job['kind'])
        except Exception as error:
            db.rollback()
            if isinstance(error,SourceWait):
                # No request was made, so a background pick gets its turn back.
                if stamped:setmeta(db,'listing_background_attempt',previous_attempt)
                if job['kind']=='catalog':
                    # The catalog is a different host with its own, longer gap: wait on that job alone.
                    db.execute('UPDATE jobs SET due=? WHERE id=?',(error.until,job['id']))
                else:
                    # Leave the job's place in the queue alone; only the budget waits.
                    setmeta(db,'listing_budget_until',error.until)
                db.commit()
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
        # A seat-photo follow-up is still sent after its alert finished or was paused.
        followup=muted and bool(db.execute('SELECT 1 FROM outbox WHERE delivered=0 AND text LIKE ? LIMIT 1',(preferences.PHOTO_MARK+'%',)).fetchone())
        ready=(not muted or followup) and not self.dry_run and load()
        setmeta(db,'notifications_mode','paused' if muted else 'enabled' if ready else 'disabled' if self.dry_run else 'waiting for local setup')
        setmeta(db,'city_state','cooldown' if float(meta(db,'source_cooldown','0'))>time.time() else 'running')
        setmeta(db,'city_heartbeat',time.time());setmeta(db,'monitor_mode','api')
        bucket=int(time.time()//900)*900
        if not db.execute('SELECT 1 FROM metric_history WHERE at=?',(bucket,)).fetchone():
            totals=coverage(db)
            db.execute('INSERT INTO metric_history VALUES(?,?,?,?)',(bucket,totals['movies'],totals['venues'],totals['sessions']))
        db.commit()
        if ready or (not muted and not self.dry_run):
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
            if ready and ((running and meta(db,'notifications_paused','0')!='1') or followup):
                delivered_id=drain(db,commit=False)
                if delivered_id:preferences.delivered(db,delivered_id)
            db.commit()
        self.stop.wait(1)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=os.environ.get('BMS_DB','runtime/api.sqlite3'))
    parser.add_argument('--seed-db');parser.add_argument('--seconds',type=int,default=0);parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args();Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    import fcntl  # Linux only; the dashboard imports this module on any platform.
    lock=open(args.db+'.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('API monitor already running')
    db=connect(args.db);seed(db,args.seed_db);db.close()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    monitor=Monitor(args.db,args.dry_run)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:monitor.stop.set())
    monitor.run(args.seconds)

if __name__=='__main__':main()
