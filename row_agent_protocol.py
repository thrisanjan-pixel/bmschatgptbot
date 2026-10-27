"""Authenticated PC-to-Railway seat observations; no browser or secrets in the cloud response."""
import hashlib
import hmac
import json
import re
import time

from api_source import seat_layout_url
from store import meta, setmeta
import row_watch
import watch_preferences as preferences


def signature(password, method, path, stamp, nonce, body=b''):
    digest=hashlib.sha256(body).hexdigest()
    message=f'{method}\n{path}\n{stamp}\n{nonce}\n{digest}'.encode()
    return hmac.new(password.encode(),message,hashlib.sha256).hexdigest()


def verify(password, method, path, headers, body, seen, now=None):
    now=time.time() if now is None else now
    stamp=headers.get('X-Row-Time','')
    nonce=headers.get('X-Row-Nonce','')
    supplied=headers.get('X-Row-Signature','')
    if not password or not re.fullmatch(r'\d{10,11}',stamp) or abs(now-int(stamp))>90:
        return False
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',nonce) or not re.fullmatch(r'[0-9a-f]{64}',supplied):
        return False
    if nonce in seen:return False
    if not hmac.compare_digest(supplied,signature(password,method,path,stamp,nonce,body)):
        return False
    seen[nonce]=now
    for key,when in list(seen.items()):
        if when<now-180:del seen[key]
    return True


def connected(db,now=None):
    now=time.time() if now is None else now
    return now-float(meta(db,'row_agent_heartbeat','0'))<90


def jobs(db,now=None):
    now=time.time() if now is None else now
    row_watch.schema(db)
    active={r['preferences']['row_show'] for r in preferences.active(db) if r['preferences'].get('row_show')}
    requested={r['show_id'] for r in db.execute('SELECT show_id FROM row_layouts WHERE due>0 AND due<=?',(now,))}
    result=[]
    for show_id in sorted(active|requested):
        row=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(show_id,now)).fetchone()
        if not row:continue
        state=db.execute('SELECT checked,due,error FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
        if state:
            if state['error'] and state['due']>now:continue
            if show_id not in requested and state['checked']>now-30:continue
        result.append(json.loads(row['payload']))
    setmeta(db,'row_agent_heartbeat',now)
    db.commit()
    return result


def _validate_result(show,data):
    if not isinstance(data,dict) or not isinstance(data.get('rows'),list) or not isinstance(data.get('available'),list):
        raise ValueError('Invalid seat observation')
    if len(data['rows'])>500 or len(data['available'])>10000 or data.get('url')!=seat_layout_url(show):
        raise ValueError('Invalid seat observation')
    categories={c['name'] for c in show['categories'].values()}
    ids=set()
    for row in data['rows']:
        if not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ('id','category','row')):
            raise ValueError('Invalid seat row')
        if row['category'] not in categories or not 1<=len(row['row'])<=32 or not row['row'].isprintable():
            raise ValueError('Invalid seat row')
        if row['id']!=row['category']+' / '+row['row'] or row['id'] in ids:raise ValueError('Invalid seat row')
        ids.add(row['id'])
    for seat in data['available']:
        if not isinstance(seat,dict) or seat.get('row_id') not in ids or not isinstance(seat.get('seat'),str):
            raise ValueError('Invalid seat')
        if not 1<=len(seat['seat'])<=20 or not seat['seat'].isprintable():raise ValueError('Invalid seat')


def observation(db,payload,now=None):
    now=time.time() if now is None else now
    if not isinstance(payload,dict) or not isinstance(payload.get('show_id'),str):raise ValueError('Invalid showtime')
    row_watch.schema(db)
    show_id=payload['show_id']
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(show_id,now)).fetchone()
    if not row:raise ValueError('Showtime expired or unknown')
    active={r['preferences']['row_show'] for r in preferences.active(db) if r['preferences'].get('row_show')}
    requested=db.execute('SELECT 1 FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
    if show_id not in active and not requested:raise ValueError('Seat layout was not requested')
    show=json.loads(row['payload'])
    if 'error' in payload:
        error=payload['error']
        if not isinstance(error,str) or not error or len(error)>200:raise ValueError('Invalid seat read error')
        db.execute("INSERT INTO row_layouts(show_id,payload,checked,error,due) VALUES(?,'{}',0,?,?) ON CONFLICT(show_id) DO UPDATE SET error=excluded.error,due=excluded.due",(show_id,error,now+300))
        setmeta(db,'row_agent_heartbeat',now);db.commit()
        return
    _validate_result(show,payload.get('data'))
    db.execute("INSERT OR IGNORE INTO row_layouts(show_id,payload) VALUES(?,'{}')",(show_id,))
    row_watch.ingest(db,show,payload['data'])
    setmeta(db,'row_agent_heartbeat',now);db.commit()
