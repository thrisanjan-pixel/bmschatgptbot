"""Independent in-service health checks; never claim to survive a host outage."""
import argparse,datetime as dt,json,os,time
from store import connect,meta,setmeta,emit
import watch_preferences as prefs

def hosting_status(now=None):
 now=time.time() if now is None else now
 raw=os.environ.get('SCREENWATCH_HOSTING_ENDS_AT','')
 try:expiry=dt.datetime.fromisoformat(raw.replace('Z','+00:00')).timestamp() if raw else 0
 except ValueError:expiry=0
 return {'provider':'Railway','allowance_connected':bool(expiry),'ends_at':expiry,
         'days_remaining':max(0,(expiry-now)/86400) if expiry else None,
         'warning':('Hosting allowance has expired' if expiry<=now else 'Hosting allowance ends within 3 days') if expiry and expiry-now<3*86400 else 'Hosting allowance expiry is not connected; check Railway billing' if not expiry else '',
         'billing_url':'https://railway.com/workspace/plans'}

def problems(db,now):
 rules=prefs.active(db);issues={}
 started=float(meta(db,'health_watch_started',str(now)))
 if now-started<120:return issues
 if now-float(meta(db,'city_heartbeat','0'))>90:issues['monitor']='The listing monitor has stopped reporting its heartbeat.'
 row_rules=[r for r in rules if r['preferences']['row_show']]
 if row_rules and now-float(meta(db,'row_reader_heartbeat','0'))>120:issues['row-worker']='The seat reader has stopped reporting its heartbeat.'
 for rule in rules:
  h=rule['health'];last=h['last_success'] or rule['created']
  if now-last>300:
   key='source:'+ (rule['preferences']['row_show'] or rule['id'])
   issues[key]='No successful check for '+', '.join(rule['preferences']['movies'])+' in over 5 minutes. '+('The source is returning errors.' if h['error'] else 'Checks may be stalled or delayed.')
 pending=db.execute('SELECT min(created),max(attempts) FROM outbox WHERE delivered=0 AND alert_id<>?',('__health__',)).fetchone()
 if pending[0] and now-pending[0]>180 and pending[1]:issues['telegram']='Telegram delivery has failed and messages are waiting for retry. This warning also depends on Telegram recovering.'
 hosting=hosting_status(now)
 if hosting['ends_at'] and hosting['ends_at']-now<3*86400:issues['hosting']=hosting['warning']+'. Check your Railway account. No automatic purchase or upgrade will be made.'
 return issues

def check(db,now=None):
 now=time.time() if now is None else now
 if not meta(db,'health_watch_started'):setmeta(db,'health_watch_started',now)
 current=problems(db,now)
 previous=json.loads(meta(db,'health_watch_issues','{}'))
 for key,message in current.items():
  if key not in previous:
   emit(db,'health:'+key+':'+str(now),'⚠️ Screenwatch needs attention\n\n'+message+'\n\nOpen the dashboard for current status. The last good results are retained.',alert_id='__health__')
 for key in previous.keys()-current.keys():
  emit(db,'recovery:'+key+':'+str(now),'✅ Screenwatch recovered\n\nThe previously reported issue has cleared: '+previous[key],alert_id='__health__')
 setmeta(db,'health_watch_issues',json.dumps(current));setmeta(db,'health_watch_heartbeat',now);db.commit()
 return current

def main():
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);a=p.parse_args();db=connect(a.db)
 from telegram import drain
 from local_credentials import load
 while True:
  try:
   check(db)
   from backup import automatic
   try:automatic(db,a.db);setmeta(db,'backup_error','');db.commit()
   except Exception:setmeta(db,'backup_error','Automatic backup failed; download a backup and check storage.');db.commit()
   if load():
    db.execute('BEGIN IMMEDIATE');drain(db,commit=False,system_only=True);db.commit()
  except Exception:
   db.rollback()
  time.sleep(15)
if __name__=='__main__':main()
