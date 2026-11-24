import argparse
import base64
import json
import os
import time
import hmac
import hashlib
import html
import re
import secrets
import sqlite3
import telegram
import threading
from http.cookies import SimpleCookie, CookieError
from urllib.parse import parse_qs,urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
DEFAULT_DB='/workspace/booking-tracker/state.sqlite3'
PWA_FILES={'/manifest.json':('manifest.json','application/manifest+json'),'/static/icon-180.png':('icon-180.png','image/png'),'/static/icon-192.png':('icon-192.png','image/png'),'/static/icon-512.png':('icon-512.png','image/png'),'/static/geist.woff2':('geist.woff2','font/woff2'),'/static/geist-mono.woff2':('geist-mono.woff2','font/woff2')}
from store import connect, coverage, meta, setmeta
import watch_preferences as preferences


def check_queue(db):
    """Live view for the dashboard: alert checks coming up next and the ones just done."""
    import api_watch,row_watch
    import watch_preferences as prefs
    now=time.time();wanted=api_watch.wanted_dates(db)
    manual=api_watch.listing_refresh(db,now)
    manual_ids=set(manual['pending'])
    def version(payload):
        shows=wanted['profiles'].get(payload.get('event')) or []
        return shows[0]['language'].replace('•','·') if shows else ''
    def listing(row):
        payload=json.loads(row['payload'])
        label=version(payload)
        return {'kind':'showtimes','movie':payload.get('movie',''),'date':payload.get('date',''),'detail':'Listing version · '+label if label else 'BookMyShow listing','checked':row['checked'],'error':row['error']}
    order=api_watch.listing_order(db,now)
    alert_rows=[r for r in order if r['watched']]
    later=sorted((r for r in db.execute('SELECT * FROM jobs WHERE due>? AND (expires=0 OR expires>?)',(now,now)) if r['id'] in manual_ids or api_watch.is_watched(json.loads(r['payload']),wanted)),key=lambda r:r['due'])
    has_requests=db.execute("SELECT 1 FROM sqlite_master WHERE name='source_requests'").fetchone()
    last=(db.execute("SELECT max(at) FROM source_requests WHERE host='in.bookmyshow.com'").fetchone()[0] if has_requests else 0) or 0
    from http_source import listing_interval,policy
    gap=policy('in.bookmyshow.com',now,listing_interval(db))[1]
    step=gap*1.25 if len(order)>len(alert_rows) else gap
    at=max(now,float(meta(db,'listing_budget_until','0')),last+gap);upcoming=[]
    for row in alert_rows+later:
        at=max(at,row['due']);upcoming.append({**listing(row),'eta':at});at+=step
    for item in row_watch.queue(db,now):
        upcoming.append({'kind':'seats','movie':item['movie'],'date':'','detail':' · '.join(filter(None,[item['venue'],item['date'],item['time']])),'alert':item['alert'],'eta':item['eta'],'checked':0,'error':''})
    upcoming.sort(key=lambda item:item['eta'])
    row_shows=set(row_watch.targets(db)[0])
    keys={r['id']:r for r in alert_rows+later}
    recent=[]
    for stat in db.execute('SELECT source,job_key,last_attempt,last_success,error FROM check_stats ORDER BY last_attempt DESC LIMIT 200'):
        if stat['source']=='listings' and stat['job_key'] in keys:item=listing(keys[stat['job_key']])
        elif stat['source']=='rows' and stat['job_key'] in row_shows:
            show=db.execute('SELECT payload FROM sessions WHERE id=?',(stat['job_key'],)).fetchone()
            show=json.loads(show[0]) if show else {}
            item={'kind':'seats','movie':show.get('movie',''),'date':show.get('date',''),'detail':' · '.join(filter(None,[show.get('venue',''),show.get('time','')]))}
        else:continue
        # Every check runs directly from the Railway server.
        recent.append({**item,'checked_via':'Railway','at':stat['last_attempt'],'ok':stat['last_success']>=stat['last_attempt'],'error':stat['error']})
        if len(recent)>=8:break
    inventory=api_watch.listing_refresh(db,now,inventory=True)
    inventory['pending']=len(inventory['pending'])
    manual['pending']=len(manual['pending'])
    return {'now':now,'upcoming':upcoming[:10],'recent':recent,'inventory_refresh':inventory,'listing_refresh':manual,
            'listing_interval':listing_interval(db),'listing_effective_interval':gap}


def listing_queue(db):
    """Read-only view of the listing jobs alerts depend on, in the order they are due."""
    from api_watch import is_watched,wanted_dates
    wanted=wanted_dates(db)
    rows=[dict(id=r['id'][-60:],movie=json.loads(r['payload']).get('movie',''),date=json.loads(r['payload']).get('date',''),due=r['due'],checked=r['checked'],expires=r['expires'],error=r['error'])
          for r in db.execute('SELECT id,payload,due,checked,expires,error FROM jobs') if is_watched(json.loads(r['payload']),wanted)]
    return sorted(rows,key=lambda r:r['due'])[:30]

def status(path):
    db=connect(path)
    try:
        heartbeat=float(meta(db,'city_heartbeat','0'))
        state=meta(db,'city_state','stopped')
        if time.time()-heartbeat>90:state='stale'
        from health_watch import hosting_status
        health_issues=json.loads(meta(db,'health_watch_issues','{}'))
        alert_health=[rule['health'] for rule in preferences.rules(db) if rule['status']=='active']
        return {'reliability':{'requests':[dict(r) for r in db.execute('SELECT at,host,endpoint,status,retry_after,cf_ray,outcome FROM source_requests ORDER BY at DESC LIMIT 12')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_requests'").fetchone() else [],'checks':[dict(r) for r in db.execute('SELECT * FROM check_stats ORDER BY last_attempt DESC LIMIT 100')],
                'listing_queue':listing_queue(db),
                'refused_alerts':sum(h['source_refused'] for h in alert_health),
                'verification_failed_alerts':sum(h['verification_failed'] for h in alert_health),
                'issues':health_issues,'health_heartbeat':float(meta(db,'health_watch_heartbeat','0')),
                'row_heartbeat':float(meta(db,'row_reader_heartbeat','0')),
                'delivery':dict(db.execute("SELECT count(*) AS pending,coalesce(max(attempts),0) AS attempts,coalesce(min(created),0) AS oldest FROM outbox WHERE delivered=0").fetchone()),
                'storage':json.loads(meta(db,'storage_usage','{}')),'backup_error':meta(db,'backup_error',''),'last_backup':float(meta(db,'last_backup','0')),'hosting':hosting_status(),'external_monitor_configured':False},'state':state,'heartbeat':heartbeat,'cooldown_until':float(meta(db,'source_cooldown','0')),
                'seat_cooldown_until':float(meta(db,'seat_cooldown_until','0')),
                'seat_source':os.environ.get('BMS_SEAT_SOURCE','server'),'seat_worker_seen':float(meta(db,'seat_worker_seen','0')),
                'seat_worker_offline':meta(db,'seat_worker_offline','0')=='1',
                'source_health':[dict(r) for r in db.execute('SELECT h.host,h.failures,h.reason,coalesce(s.not_before,0) AS not_before FROM source_health h LEFT JOIN source_hosts s ON s.host=h.host')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_health'").fetchone() else [],
                'source_budget_enabled':os.environ.get('BMS_REQUEST_BUDGET')=='1',
                'seat_budget_until':float(meta(db,'seat_budget_until','0')),
                'listing_budget_until':float(meta(db,'listing_budget_until','0')),
                'source_refusals':{kind:json.loads(meta(db,kind+'_last_refusal','{}')) for kind in ('listing','seat')},
                'background_cooldown_until':float(meta(db,'background_cooldown_until','0')),
                'listing_check_started':float(meta(db,'listing_check_started','0')),
                'cooldown_started':float(meta(db,'source_cooldown_started','0')),
                'cooldown_duration':float(meta(db,'source_cooldown_duration','0')),
                'metric_history':[dict(r) for r in db.execute('SELECT at,movies,venues,sessions FROM metric_history ORDER BY at DESC LIMIT 7')][::-1],
                'hosted':os.environ.get('BMS_PUBLIC')=='1',
                'telegram_enabled':meta(db,'telegram_enabled','1')=='1',
                'stale_warning':__import__('health_watch').stale_settings(db),
                'blind_warning':{'minutes':__import__('alert_health').blind_minutes(db)},
                'telegram_configured':telegram.configured(),
                'mode':meta(db,'monitor_mode','browser'),
                'last_success':float(meta(db,'api_last_success',meta(db,'catalog_last_success','0'))),
                'recent_alerts':[dict(r) for r in db.execute('SELECT text,created,delivered,attempts,alert_id FROM outbox ORDER BY created DESC LIMIT 12')],
                'delivered_alerts':db.execute('SELECT count(*) FROM outbox WHERE delivered>0').fetchone()[0],
                'movie_details':[{'title':r['title'],'url':r['url'],'image':meta(db,'poster:'+r['url'])} for r in db.execute('SELECT title,url FROM inventory ORDER BY title')],
                'notifications_mode':meta(db,'notifications_mode','unknown'),
                'telegram_setup_test_at':float(meta(db,'telegram_setup_test_at','0')),
                'source_errors':[dict(r) for r in db.execute("SELECT kind,error FROM jobs WHERE error<>'' ORDER BY due DESC LIMIT 5")],
                'coverage':coverage(db),'catalog_last_success':float(meta(db,'catalog_last_success','0')),
                'movies':[dict(r) for r in db.execute('SELECT title,url,seen FROM inventory ORDER BY title')]}
    finally:db.close()


def row_worker_enabled():
    return os.environ.get('BMS_SEAT_SOURCE','server')=='worker' and os.environ.get('BMS_ROWS_ENABLED')=='1'


def handler(path, public=False):
    passwordless=public and os.environ.get('SCREENWATCH_PASSWORDLESS')=='1'
    csrf=secrets.token_urlsafe(32)
    setup_lock=threading.Lock()
    sessions={}
    login_failures=[]
    auth_lock=threading.Lock()
    if public:
        db=connect(path)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS web_sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL)')
            db.commit()
        finally:db.close()
    def session_hash(password,token):
        return hmac.new(password.encode(),token.encode(),hashlib.sha256).hexdigest()
    class Handler(BaseHTTPRequestHandler):
        def authenticated(self):
            if not public or passwordless:return True
            password=os.environ.get('DASHBOARD_PASSWORD','')
            expected='Basic '+base64.b64encode(('screenwatch:'+password).encode()).decode()
            if password and hmac.compare_digest(self.headers.get('Authorization',''),expected):return True
            try:
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                token=cookies['screenwatch_session'].value if 'screenwatch_session' in cookies else ''
            except CookieError:return False
            if not password or not token or len(token)>100:return False
            digest=session_hash(password,token)
            with auth_lock:
                if sessions.get(digest,0)>time.time():return True
            db=connect(path)
            try:row=db.execute('SELECT expires FROM web_sessions WHERE token_hash=?',(digest,)).fetchone()
            finally:db.close()
            if not row or row[0]<=time.time():return False
            with auth_lock:sessions[digest]=row[0]
            return True
        def authorized(self):
            if self.authenticated():return True
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="Screenwatch", charset="UTF-8"')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            return False
        def login_page(self,error='',code=200):
            self.reply('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Screenwatch</title>
<style>body{margin:0;background:#10151f;color:#f1f0ed;font:16px system-ui;display:grid;place-items:center;min-height:100vh}main{width:min(360px,80vw);padding:36px;background:#171e2b;border:1px solid #303b4e;border-radius:20px;box-shadow:0 12px 40px #17233b0a}small{color:#d8fa85;font-weight:700;letter-spacing:.12em}h1{font-size:30px;margin-bottom:8px}p{color:#8e9aae;line-height:1.7}label{display:block;margin:24px 0 8px}input,button{box-sizing:border-box;width:100%;padding:14px;border-radius:10px;font:inherit}input{border:1px solid #3b4860;background:#101722;color:#f1f0ed}button{margin-top:18px;background:#d8fa85;color:#172416;border:0;font-weight:700;cursor:pointer}.error{color:#a42d24}</style>
<main><small>SCREENWATCH</small><h1>Your next show.</h1><p>Sign in to manage your personal movie alerts.</p><p class="error">'''+html.escape(error)+'''</p><form action="/login" method="post"><input type="hidden" name="csrf" value="'''+csrf+'''"><label for="password">Website password</label><input id="password" type="password" name="password" required autocomplete="current-password" autofocus><button>Sign in</button></form></main></html>''',code)
        def reply(self,body,code=200):
            body=body.encode()
            self.send_response(code)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def reply_json(self,value,code=200):
            body=json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def local_host(self):
            return self.headers.get('Host','') in (f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}')
        def worker_request(self):
            """Home seat worker API: bearer BMS_WORKER_TOKEN, used instead of the website login."""
            token=os.environ.get('BMS_WORKER_TOKEN','')
            if len(token)<32 or not row_worker_enabled():self.reply_json({'error':'Seat worker is not enabled on this server'},404);return
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):self.reply_json({'error':'Unauthorized'},401);return
            import row_watch
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=4_000_000:raise ValueError('Request too large')
                data=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(data,dict):raise ValueError('Invalid request')
                db=connect(path)
                try:
                    if self.path=='/api/worker/claim':self.reply_json({'job':row_watch.claim(db)});return
                    show_id=data.get('show_id')
                    if not isinstance(show_id,str):raise ValueError('Missing showtime')
                    row_watch.submit(db,show_id,data.get('data'),data.get('error'))
                finally:db.close()
            except sqlite3.Error:self.reply_json({'error':'Database busy'},503);return
            except (ValueError,TypeError,KeyError) as e:self.reply_json({'error':str(e)[:200]},400);return
            self.reply_json({'ok':True})
        def do_POST(self):
            if self.path in ('/api/worker/claim','/api/worker/result'):
                self.worker_request();return
            if passwordless and self.path=='/login':
                self.send_response(303);self.send_header('Location','/');self.end_headers();return
            if public and self.path=='/login':
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=4096:raise ValueError()
                    data=parse_qs(self.rfile.read(length).decode())
                    if not hmac.compare_digest(data.get('csrf',[''])[0],csrf):raise ValueError()
                except (ValueError,UnicodeError):self.reply('Reload the sign-in page and try again.',403);return
                with auth_lock:
                    now=time.time()
                    login_failures[:]=[t for t in login_failures if t>now-60]
                    if len(login_failures)>=10:self.login_page('Too many attempts. Try again in one minute.',429);return
                    password=os.environ.get('DASHBOARD_PASSWORD','')
                    if not password or not hmac.compare_digest(data.get('password',[''])[0].encode(),password.encode()):
                        login_failures.append(now);self.login_page('Password incorrect. Try again.',401);return
                    for key in list(sessions):
                        if sessions[key]<=now:del sessions[key]
                    if len(sessions)>=100:sessions.pop(next(iter(sessions)))
                    session=secrets.token_urlsafe(32);digest=session_hash(password,session)
                    db=connect(path)
                    try:
                        db.execute('DELETE FROM web_sessions WHERE expires<=?',(now,))
                        if db.execute('SELECT count(*) FROM web_sessions').fetchone()[0]>=100:
                            db.execute('DELETE FROM web_sessions WHERE token_hash=(SELECT token_hash FROM web_sessions ORDER BY expires LIMIT 1)')
                        db.execute('INSERT INTO web_sessions VALUES(?,?)',(digest,now+28800));db.commit()
                    finally:db.close()
                    sessions[digest]=now+28800
                self.send_response(303)
                self.send_header('Location','/')
                self.send_header('Set-Cookie','screenwatch_session='+session+'; Path=/; Max-Age=28800; HttpOnly; Secure; SameSite=Strict')
                self.send_header('Cache-Control','no-store')
                self.end_headers();return
            if self.path=='/api/rows':
                if not self.authorized():return
                if os.environ.get('BMS_ROWS_ENABLED')!='1':self.reply('Row reader is unavailable on this host.',503);return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<2048:raise ValueError('Invalid request')
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict):raise ValueError('Invalid request')
                    show_id=data.get('show_id')
                    if not isinstance(show_id,str):raise ValueError('Choose a showtime')
                    import row_watch
                    db=connect(path)
                    try:row_watch.request(db,show_id)
                    finally:db.close()
                except sqlite3.Error:self.reply('Seat reader is busy. Please retry shortly.',503);return
                except (ValueError,TypeError) as e:self.reply(html.escape(str(e)),400);return
                self.reply('Loading real rows for this show.',202);return
            if self.path=='/api/blind-settings':
                if not self.authorized():return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                import alert_health
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<64:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict) or set(data)!={'minutes'} or type(data['minutes']) is not int or data['minutes'] not in alert_health.BLIND_MINUTES:raise ValueError()
                    db=connect(path)
                    try:
                        db.execute('BEGIN IMMEDIATE');setmeta(db,'blind_warning_minutes',str(data['minutes']));db.commit()
                    finally:db.close()
                except (ValueError,TypeError,sqlite3.Error):self.reply(json.dumps({'error':'Could not save the warning setting. Retry.'}),400);return
                self.reply(json.dumps({'minutes':data['minutes']}));return
            if self.path=='/api/seat-retry':
                if not self.authorized():return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                import row_watch
                db=connect(path)
                try:row_watch.clear_source_wait(db)
                except sqlite3.Error:self.reply('Seat reader is busy. Please retry shortly.',503);return
                finally:db.close()
                self.reply(json.dumps({'seat_ready_at':0}));return
            if self.path=='/api/seat-queue':
                if not self.authorized():return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<2048:raise ValueError('Invalid request')
                    data=json.loads(self.rfile.read(length))
                    show_id=data.get('show_id') if isinstance(data,dict) else None
                    if not isinstance(show_id,str):raise ValueError('Choose a showtime')
                    import row_watch
                    db=connect(path)
                    try:row_watch.dequeue(db,show_id);items=row_watch.queue(db)
                    finally:db.close()
                except sqlite3.Error:self.reply('Seat reader is busy. Please retry shortly.',503);return
                except (ValueError,TypeError) as e:self.reply(html.escape(str(e)),400);return
                self.reply_json({'queue':items});return
            if self.path=='/api/health-settings':
                if not self.authorized():return
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                import health_watch
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<256:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict) or set(data)!={'enabled','minutes'} or type(data['enabled']) is not bool or data['minutes'] not in health_watch.STALE_MINUTES:raise ValueError()
                    db=connect(path)
                    try:
                        db.execute('BEGIN IMMEDIATE')
                        setmeta(db,'stale_warning_enabled','1' if data['enabled'] else '0');setmeta(db,'stale_warning_minutes',str(data['minutes']))
                        # Forget stalled-check warnings already sent, so a settings change is not announced
                        # as a recovery; anything still stalled is reported again under the new setting.
                        issues={k:v for k,v in json.loads(meta(db,'health_watch_issues','{}')).items() if not (k.startswith('source:') or k=='seat-rate-limit')}
                        setmeta(db,'health_watch_issues',json.dumps(issues));db.commit()
                    finally:db.close()
                except (ValueError,TypeError,sqlite3.Error):self.reply(json.dumps({'error':'Could not save the warning setting. Retry.'}),400);return
                self.reply(json.dumps({'enabled':data['enabled'],'minutes':data['minutes']}));return
            if self.path=='/api/telegram':
                if not self.authorized():return
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<256:raise ValueError()
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict) or set(data)!={'enabled'} or type(data['enabled']) is not bool:raise ValueError()
                    if data['enabled'] and public and not telegram.configured():raise ValueError()
                    db=connect(path)
                    try:
                        db.execute('BEGIN IMMEDIATE');setmeta(db,'telegram_enabled','1' if data['enabled'] else '0');db.commit()
                    finally:db.close()
                except (ValueError,TypeError,sqlite3.Error):self.reply(json.dumps({'error':'Could not change Telegram. Check connection and retry.'}),400);return
                self.reply(json.dumps({'enabled':data['enabled']}));return
            if self.path=='/api/restore':
                if not self.authorized():return
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                try:
                    from backup import restore,MAX_BYTES
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=MAX_BYTES:raise ValueError('Backup must be smaller than 32 MB')
                    db=connect(path)
                    try:result=restore(db,self.rfile.read(length))
                    finally:db.close()
                except (ValueError,TypeError,sqlite3.Error):self.reply(json.dumps({'error':'Invalid backup or incompatible schema. Nothing was restored.'}),400);return
                self.reply(json.dumps(result));return
            if self.path=='/api/watch':
                if not self.authorized():return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                    self.reply('Request rejected.',403);return
                db=None
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=65536:raise ValueError('Invalid request size')
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict):raise ValueError('Invalid settings')
                    if public and data.get('enabled') and not telegram.configured():raise ValueError('Connect Telegram before enabling alerts')
                    if data.get('enabled') and data.get('row_show') and os.environ.get('BMS_ROWS_ENABLED')!='1':raise ValueError('Row monitoring is unavailable on this host')
                    db=connect(path)
                    requested=data.pop('alert_id',None)
                    action=data.pop('action','')
                    if action in ('refresh_listings','refresh_inventory'):
                        if data or requested is not None:raise ValueError('Invalid refresh request')
                        from api_watch import listing_refresh
                        refresh=listing_refresh(db,time.time(),queue=True,inventory=action=='refresh_inventory')
                        refresh['pending']=len(refresh['pending'])
                        self.reply(json.dumps(refresh));return
                    if action=='set_listing_interval':
                        if requested is not None or set(data)!={'seconds'}:raise ValueError('Invalid cadence request')
                        from api_watch import set_listing_interval
                        set_listing_interval(db,data['seconds'])
                        self.reply(json.dumps({'seconds':data['seconds']}));return
                    if action=='register_movie':
                        if set(data)!={'url'} or not isinstance(data['url'],str):raise ValueError('Provide a BookMyShow movie link')
                        from api_watch import register_movie
                        title=register_movie(db,data['url']);db.commit()
                        self.reply(json.dumps({'title':title}));return
                    if action=='save_preset':
                        if set(data)!={'name','preferences'}:raise ValueError('Provide a preset name and preferences')
                        self.reply(json.dumps({'presets':preferences.save_preset(db,data['name'],data['preferences'])}));return
                    if action=='delete_preset':
                        if set(data)!={'id'}:raise ValueError('Choose a preset to delete')
                        self.reply(json.dumps({'presets':preferences.delete_preset(db,data['id'])}));return
                    if action in ('pc_opened','pc_cancel'):
                        if set(data)!={'alert'}:raise ValueError('Choose an alert')
                        preferences.set_open_on_pc(db,data['alert'],False)
                        self.reply(json.dumps({'ok':True}));return
                    if action=='delete':
                        if data:raise ValueError('Invalid delete request')
                        preferences.delete(db,requested)
                        self.reply(json.dumps({'deleted':True}));return
                    if action in ('cancel','reset','booked'):
                        if data:raise ValueError('Invalid alert action request')
                        if action=='reset' and public and not telegram.configured():raise ValueError('Connect Telegram before enabling alerts')
                        if action=='reset' and preferences.read(db,requested).get('row_show') and os.environ.get('BMS_ROWS_ENABLED')!='1':raise ValueError('Row monitoring is unavailable on this host')
                        saved_id=preferences.transition(db,requested,action)
                        self.reply(json.dumps({'id':saved_id}));return
                    if action:raise ValueError('Invalid alert action')
                    create=data.pop('create',False)
                    if requested is not None and not isinstance(requested,str):raise ValueError('Invalid alert ID')
                    if type(create) is not bool:raise ValueError('Invalid alert request')
                    saved_id=preferences.save(db,data,requested,create)
                except (ValueError,TypeError) as error:
                    self.reply(json.dumps({'error':'Settings not saved: '+str(error)}),400);return
                except sqlite3.Error:
                    self.reply(json.dumps({'error':'The alert database is busy or unavailable. Please try again.'}),503);return
                finally:
                    if db is not None:db.close()
                self.reply(json.dumps({'id':saved_id}));return
            if public:self.reply('Setup is disabled on the hosted dashboard.',403);return
            # The in-app browser can forward localhost through 127.0.0.1.
            # Require a local Host and the unguessable form token; permit both
            # loopback names as Origin and transports that omit Origin.
            origins=(None,'null',f'http://localhost:{self.server.server_port}',f'http://127.0.0.1:{self.server.server_port}')
            if self.path!='/setup' or not self.local_host() or self.headers.get('Origin') not in origins:
                self.reply('Request rejected.',403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192:raise ValueError()
                data=parse_qs(self.rfile.read(length).decode())
                if not hmac.compare_digest(data.get('csrf',[''])[0],csrf):raise ValueError()
                token=data.get('token',[''])[0].strip();chat=data.get('chat',[''])[0].strip()
                if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token) or not re.fullmatch(r'-?\d+|@[A-Za-z0-9_]+',chat):raise ValueError()
            except (ValueError,UnicodeError):
                self.reply('Invalid setup request. Return to /setup and try again.',400);return
            from local_credentials import KEYS,save
            from telegram import request,send,DeliveryError
            with setup_lock:
                previous={k:os.environ.get(k) for k in KEYS}
                try:
                    values=dict(zip(KEYS,(token,chat)))
                    os.environ.update(values)
                    request('getMe',{})
                    send('Setup test: Hyderabad Booking Watch is connected. This is a setup test, not a new show or seat release.')
                    save(values)
                    db=connect(path)
                    try:
                        setmeta(db,'telegram_setup_test_at',time.time());db.commit()
                    finally:db.close()
                except (DeliveryError,OSError):
                    self.reply('Telegram setup failed. Check the token, chat ID, and that you have started the bot. No credentials are displayed.',400)
                    return
                finally:
                    for k,v in previous.items():
                        if v is None:os.environ.pop(k,None)
                        else:os.environ[k]=v
            self.reply('Telegram setup test delivered. The local monitor will enable notifications shortly. <a href="/">Open dashboard</a>')
        def do_GET(self):
            if passwordless and self.path=='/login':
                self.send_response(303);self.send_header('Location','/');self.end_headers();return
            if self.path=='/readyz':
                data=status(path);r=data['reliability'];now=time.time()
                ok=now-data['heartbeat']<90 and now-r['health_heartbeat']<90 and not r['issues']
                self.reply('ready' if ok else 'monitor needs attention',200 if ok else 503);return
            if self.path=='/healthz':
                self.reply('ok');return
            asset=PWA_FILES.get(urlsplit(self.path).path)
            if asset:
                # Public on purpose: iOS fetches the manifest and home-screen icon without the session cookie. Fixed file names only.
                body=Path(__file__).with_name('static').joinpath(asset[0]).read_bytes()
                self.send_response(200);self.send_header('Content-Type',asset[1]);self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','public, max-age=86400');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body);return
            if public and self.path in ('/','/login') and not self.authenticated():
                self.login_page();return
            if not self.authorized():return
            if self.path=='/setup':
                if public:self.reply('Manage Telegram in Railway service variables.',403);return
                if not self.local_host():self.reply('Local access only.',403);return
                self.reply('''<!doctype html><meta name="viewport" content="width=device-width"><title>Local Telegram setup</title>
<style>body{font:18px system-ui;max-width:650px;margin:50px auto;padding:20px;background:#101311;color:#f5f7f0}input{display:block;width:95%;padding:12px;margin:12px 0}button{padding:12px}</style>
<h1>Connect Telegram on this PC</h1><p>Enter your rotated bot token and chat ID here, never in chat. Start your bot in Telegram first. Submitting sends one labelled setup test to that chat.</p>
<p>Credentials are saved outside the project in your Linux home with owner-only permissions. Nothing is committed to GitHub.</p>
<form method="post" action="/setup"><input type="hidden" name="csrf" value="'''+csrf+'''">
<label>Bot token<input type="password" name="token" required autocomplete="off"></label><label>Chat ID<input name="chat" required autocomplete="off"></label>
<button>Save locally and send setup test</button></form>''');return
            if urlsplit(self.path).path=='/api/rows':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                import row_watch
                show_id=parse_qs(urlsplit(self.path).query).get('show',[''])[0]
                db=connect(path)
                try:body=json.dumps(row_watch.get(db,show_id)).encode()
                finally:db.close()
                content_type='application/json'
            elif self.path=='/api/check-queue':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                db=connect(path)
                try:body=json.dumps(check_queue(db)).encode()
                finally:db.close()
                content_type='application/json'
            elif self.path=='/api/seat-queue':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                import row_watch
                db=connect(path)
                try:body=json.dumps({'queue':row_watch.queue(db),'seat_ready_at':row_watch.source_ready_at(db)}).encode()
                finally:db.close()
                content_type='application/json'
            elif self.path=='/api/backup':
                from backup import export
                db=connect(path)
                try:body=json.dumps(export(db),ensure_ascii=False).encode()
                finally:db.close()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Disposition','attachment; filename="screenwatch-backup.json"');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            elif urlsplit(self.path).path=='/api/watch':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                import row_watch
                db=connect(path)
                try:
                    alert_id=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
                    alerts=preferences.rules(db)
                    new=parse_qs(urlsplit(self.path).query).get('new',[''])[0]=='1'
                    selected=next((a for a in alerts if a['id']==alert_id),None) if alert_id else (alerts[0] if alerts and not new else None)
                    recent=preferences.history(db)
                    for a in alerts:a['history']=recent.get(a['id'],[])
                    body=json.dumps({'preferences':selected['preferences'] if selected else preferences.defaults(),
                        'selected_id':selected['id'] if selected else '',
                        'alerts':alerts,'options':preferences.options(db),'csrf':csrf,
                        'skipped':int(meta(db,'watch_skipped','0')),
                        'completed':selected['completed'] if selected else 0,
                        'rows_supported':os.environ.get('BMS_ROWS_ENABLED')=='1',
                        'pc_clients':preferences.pc_clients(db),'presets':preferences.presets(db),'row_any_limit':preferences.ROW_ANY_LIMIT,
                        'seat_ready_at':row_watch.source_ready_at(db)}).encode()
                finally:db.close()
                content_type='application/json'
            elif urlsplit(self.path).path=='/api/pc-alerts':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                try:since=float(parse_qs(urlsplit(self.path).query).get('since',['0'])[0])
                except ValueError:since=0.0
                db=connect(path)
                client=parse_qs(urlsplit(self.path).query).get('client',[''])[0]
                try:body=json.dumps(preferences.pc_alerts(db,since,client=client)).encode()
                finally:db.close()
                content_type='application/json'
            elif self.path=='/api/status':
                body=json.dumps(status(path)).encode();content_type='application/json'
            elif self.path=='/':
                body=Path(__file__).with_name('static').joinpath('screenwatch.html').read_bytes();content_type='text/html; charset=utf-8'
            else:
                self.send_error(404);return
            self.send_response(200)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' https://assets-in.bmscdn.com; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    return Handler


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',default=os.environ.get('BMS_DB',DEFAULT_DB));p.add_argument('--port',type=int,default=int(os.environ.get('PORT','8000')))
    a=p.parse_args();public=os.environ.get('BMS_PUBLIC')=='1'
    if public and os.environ.get('SCREENWATCH_PASSWORDLESS')!='1' and len(os.environ.get('DASHBOARD_PASSWORD',''))<16:raise SystemExit('Set DASHBOARD_PASSWORD to at least 16 characters before hosting.')
    ThreadingHTTPServer(('0.0.0.0' if public else '127.0.0.1',a.port),handler(a.db,public)).serve_forever()
