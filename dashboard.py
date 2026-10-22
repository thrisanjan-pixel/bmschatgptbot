import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from city_watch import DEFAULT_DB
from store import connect, coverage, meta


def status(path):
    db=connect(path)
    try:
        heartbeat=float(meta(db,'city_heartbeat','0'))
        state=meta(db,'city_state','stopped')
        if time.time()-heartbeat>90:state='stale'
        return {'state':state,'heartbeat':heartbeat,'cooldown_until':float(meta(db,'source_cooldown','0')),
                'coverage':coverage(db),'catalog_last_success':float(meta(db,'catalog_last_success','0')),
                'movies':[dict(r) for r in db.execute('SELECT title,url,seen FROM inventory ORDER BY title')]}
    finally:db.close()


def handler(path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/api/status':
                body=json.dumps(status(path)).encode();content_type='application/json'
            elif self.path=='/':
                body=Path(__file__).with_name('static').joinpath('index.html').read_bytes();content_type='text/html; charset=utf-8'
            else:
                self.send_error(404);return
            self.send_response(200)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    return Handler


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',default=os.environ.get('BMS_DB',DEFAULT_DB));p.add_argument('--port',type=int,default=8000)
    a=p.parse_args();ThreadingHTTPServer(('127.0.0.1',a.port),handler(a.db)).serve_forever()
