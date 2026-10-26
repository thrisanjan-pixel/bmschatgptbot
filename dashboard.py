import argparse
import base64
import json
import os
import time
import hmac
import html
import re
import secrets
import threading
from urllib.parse import parse_qs,urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
DEFAULT_DB='/workspace/booking-tracker/state.sqlite3'
from store import connect, coverage, meta, setmeta
import watch_preferences as preferences


def status(path):
    db=connect(path)
    try:
        heartbeat=float(meta(db,'city_heartbeat','0'))
        state=meta(db,'city_state','stopped')
        if time.time()-heartbeat>90:state='stale'
        return {'state':state,'heartbeat':heartbeat,'cooldown_until':float(meta(db,'source_cooldown','0')),
                'mode':meta(db,'monitor_mode','browser'),
                'last_success':float(meta(db,'api_last_success',meta(db,'catalog_last_success','0'))),
                'recent_alerts':[dict(r) for r in db.execute('SELECT text,created,delivered,attempts FROM outbox ORDER BY created DESC LIMIT 12')],
                'delivered_alerts':db.execute('SELECT count(*) FROM outbox WHERE delivered>0').fetchone()[0],
                'shows':[json.loads(r[0]) for r in db.execute('SELECT payload FROM sessions WHERE active=1 AND cutoff>? ORDER BY cutoff LIMIT 300',(time.time(),))],
                'movie_details':[{'title':r['title'],'url':r['url'],'image':meta(db,'poster:'+r['url'])} for r in db.execute('SELECT title,url FROM inventory ORDER BY title')],
                'notifications_mode':meta(db,'notifications_mode','unknown'),
                'telegram_setup_test_at':float(meta(db,'telegram_setup_test_at','0')),
                'source_errors':[dict(r) for r in db.execute("SELECT kind,error FROM jobs WHERE error<>'' ORDER BY due DESC LIMIT 5")],
                'coverage':coverage(db),'catalog_last_success':float(meta(db,'catalog_last_success','0')),
                'movies':[dict(r) for r in db.execute('SELECT title,url,seen FROM inventory ORDER BY title')]}
    finally:db.close()


def handler(path, public=False):
    csrf=secrets.token_urlsafe(32)
    setup_lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def authorized(self):
            if not public:return True
            password=os.environ.get('DASHBOARD_PASSWORD','')
            expected='Basic '+base64.b64encode(('screenwatch:'+password).encode()).decode()
            if password and hmac.compare_digest(self.headers.get('Authorization',''),expected):return True
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="Screenwatch", charset="UTF-8"')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            return False
        def reply(self,body,code=200):
            body=body.encode()
            self.send_response(code)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def local_host(self):
            return self.headers.get('Host','') in (f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}')
        def do_POST(self):
            if self.path=='/api/rows':
                if not self.authorized():return
                if os.environ.get('BMS_ROWS_ENABLED')!='1':self.reply('Row reader is unavailable on this host.',503);return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):self.reply('Request rejected.',403);return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<2048:raise ValueError('Invalid request')
                    show_id=json.loads(self.rfile.read(length)).get('show_id')
                    if not isinstance(show_id,str):raise ValueError('Choose a showtime')
                    import row_watch
                    db=connect(path)
                    try:row_watch.request(db,show_id)
                    finally:db.close()
                except (ValueError,TypeError) as e:self.reply(html.escape(str(e)),400);return
                self.reply('Loading real rows for this show.',202);return
            if self.path=='/api/watch':
                if not self.authorized():return
                if (not public and not self.local_host()) or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),csrf):
                    self.reply('Request rejected.',403);return
                db=None
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=65536:raise ValueError('Invalid request size')
                    data=json.loads(self.rfile.read(length))
                    if data.get('enabled') and data.get('row_show') and os.environ.get('BMS_ROWS_ENABLED')!='1':raise ValueError('Row monitoring is unavailable on this host')
                    db=connect(path);preferences.save(db,data)
                except (ValueError,TypeError) as error:
                    self.reply('Settings not saved: '+html.escape(str(error)),400);return
                finally:
                    if db is not None:db.close()
                self.reply('Watch saved. Alerts follow your selected filters.');return
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
            if self.path=='/healthz':
                self.reply('ok');return
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
            elif self.path=='/api/watch':
                if not public and not self.local_host():self.reply('Local access only.',403);return
                db=connect(path)
                try:body=json.dumps({'preferences':preferences.read(db),'options':preferences.options(db),'csrf':csrf,'skipped':int(meta(db,'watch_skipped','0')),'completed':float(meta(db,'watch_completed','0')),'rows_supported':os.environ.get('BMS_ROWS_ENABLED')=='1'}).encode()
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
    if public and len(os.environ.get('DASHBOARD_PASSWORD',''))<16:raise SystemExit('Set DASHBOARD_PASSWORD to at least 16 characters before hosting.')
    ThreadingHTTPServer(('0.0.0.0' if public else '127.0.0.1',a.port),handler(a.db,public)).serve_forever()
