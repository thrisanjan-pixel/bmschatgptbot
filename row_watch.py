"""Durable on-demand row loads and polling of one selected show."""
import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path
from store import connect,meta,setmeta,emit
import watch_preferences as prefs

def schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS row_layouts(show_id TEXT PRIMARY KEY,payload TEXT,checked REAL DEFAULT 0,error TEXT DEFAULT '',due REAL DEFAULT 0)")

def request(db,show_id):
    schema(db)
    row=db.execute('SELECT payload FROM sessions WHERE id=? AND active=1 AND cutoff>?',(show_id,time.time())).fetchone()
    if not row:raise ValueError('Choose a future showtime from the list')
    db.execute("INSERT INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?) ON CONFLICT(show_id) DO UPDATE SET due=CASE WHEN checked<? THEN excluded.due ELSE due END",(show_id,time.time(),time.time()-60))
    db.commit()

def get(db,show_id):
    schema(db);row=db.execute('SELECT * FROM row_layouts WHERE show_id=?',(show_id,)).fetchone()
    return {**dict(row),'data':json.loads(row['payload'])} if row else {'show_id':show_id,'checked':0,'error':'','data':{}}

def run_reader(show,timeout=75):
    command=['xvfb-run','-a',sys.executable,str(Path(__file__).with_name('row_reader.py'))]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        stdout,stderr=process.communicate(json.dumps(show),timeout=timeout)
        return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
    except subprocess.TimeoutExpired:
        # Killing xvfb-run alone leaves its browser/display children alive.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.communicate()
        raise ValueError('Seat layout timed out. Try loading it again later.') from None

def ingest(db,show,result):
    schema(db);old=get(db,show['id']);previous=old['data'];p=prefs.read(db)
    known={r['id']:r for r in previous.get('rows',[])};known.update({r['id']:r for r in result['rows']});result['rows']=list(known.values())
    selected=set(p.get('rows',[]));before={(s['row_id'],s['seat']) for s in previous.get('available',[])}
    added=[s for s in result['available'] if s['row_id'] in selected and (s['row_id'],s['seat']) not in before]
    if old['checked'] and p['enabled'] and p.get('row_show')==show['id'] and meta(db,'notifications_paused','0')!='1' and added:
        if time.time()-float(meta(db,'watch_last_alert','0'))>=p['interval_minutes']*60 and not db.execute('SELECT 1 FROM outbox WHERE delivered=0 LIMIT 1').fetchone():
            lines=['Seats now available in '+r+': '+', '.join(s['seat'] for s in added if s['row_id']==r) for r in sorted({s['row_id'] for s in added})]
            message=f"🎬 {show['movie']}\n📍 {show['venue']}\n{show['date']} · {show['time']}\n\n"+'\n'.join(lines)
            emit(db,'row:'+show['id']+':'+str(time.time_ns()),message[:3300]+'\n\n'+result['url']);setmeta(db,'watch_last_alert',time.time())
    db.execute('UPDATE row_layouts SET payload=?,checked=?,error=?,due=0 WHERE show_id=?',(json.dumps(result),time.time(),'',show['id']));db.commit()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);args=parser.parse_args()
    if os.environ.get('BMS_ROWS_ENABLED')!='1':return
    db=connect(args.db);schema(db);db.commit();stop=False
    def halt(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,halt);signal.signal(signal.SIGINT,halt)
    while not stop:
        p=prefs.read(db);now=time.time()
        if p['enabled'] and p.get('row_show') and meta(db,'notifications_paused','0')!='1':
            db.execute('UPDATE row_layouts SET due=? WHERE show_id=? AND due=0 AND checked<?',(now,p['row_show'],now-60));db.commit()
        job=db.execute('SELECT * FROM row_layouts WHERE due>0 AND due<=? ORDER BY due LIMIT 1',(now,)).fetchone()
        if not job:time.sleep(1);continue
        row=db.execute('SELECT payload FROM sessions WHERE id=? AND cutoff>?',(job['show_id'],now)).fetchone()
        if not row:
            db.execute("UPDATE row_layouts SET due=0,error='Showtime has expired' WHERE show_id=?",(job['show_id'],));db.commit();continue
        show=json.loads(row[0])
        try:
            run=run_reader(show)
            result=json.loads(run.stdout)
            if run.returncode or result.get('error'):raise ValueError(result.get('error','Seat read failed'))
            ingest(db,show,result['data'])
        except Exception as e:
            db.execute('UPDATE row_layouts SET error=?,due=? WHERE show_id=?',(str(e).splitlines()[0][:200],time.time()+300,job['show_id']));db.commit()
    db.close()
if __name__=='__main__':main()
