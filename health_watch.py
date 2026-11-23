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

STALE_MINUTES=(15,30,60,120,240)

def stale_settings(db):
 """Whether to warn when an alert goes without a successful check, and after how long."""
 minutes=int(meta(db,'stale_warning_minutes','15') or 15)
 return {'enabled':meta(db,'stale_warning_enabled','1')!='0','minutes':minutes if minutes in STALE_MINUTES else 15}

def problems(db,now):
 rules=prefs.active(db);issues={}
 started=float(meta(db,'health_watch_started',str(now)))
 if now-started<120:return issues
 if now-float(meta(db,'city_heartbeat','0'))>90:issues['monitor']='The listing monitor has stopped reporting its heartbeat.'
 row_rules=[r for r in rules if r['preferences']['row_show']]
 if row_rules and now-float(meta(db,'row_reader_heartbeat','0'))>120:issues['row-worker']='The seat reader has stopped reporting its heartbeat.'
 rate_limited_movies=set()
 # Row reads share one 120 s gap and a 30/hour budget, so the quiet period grows with the number of watched showtimes.
 stale=stale_settings(db);quiet=stale['minutes']*60
 limit=max(quiet,240*sum(r['row_targets'] for r in row_rules))
 # Only a real source cooldown (429) excuses stale seat checks. A routine budget wait lasts about
 # a minute; counting it made the warning clear and return on every request gap.
 cooling=float(meta(db,'seat_cooldown_until','0'))>now
 for rule in rules if stale['enabled'] else []:
  h=rule['health'];last=h['last_success'] or rule['created']
  if rule['preferences']['row_show'] and not rule['row_targets']:continue  # an any-showtime alert with no matching showtime has nothing to read
  if now-last>(limit if rule['preferences']['row_show'] else quiet):
   if rule['preferences']['row_show'] and cooling:continue
   if rule['preferences']['row_show'] and h['error'] in ('Seat source HTTP 429','Seat metadata HTTP 429'):
    rate_limited_movies.update(rule['preferences']['movies'])
    continue
   target = rule['preferences']['row_show'] or '|'.join(sorted(prefs.normalized(m) for m in rule['preferences']['movies']))
   key='source:'+target
   detail=' Latest check: '+h['error']+'.' if h['error'] else ''
   issues[key]='No successful check for '+', '.join(rule['preferences']['movies'])+' in over '+str(int((limit if rule['preferences']['row_show'] else quiet)/60))+' minutes.'+detail
 if rate_limited_movies:
  issues['seat-rate-limit']='BookMyShow rate-limited seat checks. No successful seat check in over '+str(stale['minutes'])+' minutes for '+', '.join(sorted(rate_limited_movies))+'.'
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
 already_reported_seat_limit = any(
  key.startswith('source:') and 'Seat ' in message and 'HTTP 429' in message
  for key,message in previous.items()
 )
 for key,message in current.items():
  if key=='seat-rate-limit' and already_reported_seat_limit:continue
  if key not in previous:
   emit(db,'health:'+key+':'+str(now),'⚠️ Screenwatch needs attention\n\n'+message+'\n\nOpen the dashboard for current status. The last good results are retained.',alert_id='__health__')
 watched={'source:'+(r['preferences']['row_show'] or '|'.join(sorted(prefs.normalized(m) for m in r['preferences']['movies']))) for r in prefs.active(db)}
 for key in previous.keys()-current.keys():
  if key.startswith('source:') and 'HTTP 429' in previous[key] and 'seat-rate-limit' in current:continue
  # The alert was paused, deleted or finished: its checks did not recover, so say nothing.
  if key.startswith('source:') and key not in watched:continue
  emit(db,'recovery:'+key+':'+str(now),'✅ Screenwatch recovered\n\nThe previously reported issue has cleared: '+previous[key],alert_id='__health__')
 setmeta(db,'health_watch_issues',json.dumps(current));setmeta(db,'health_watch_heartbeat',now);db.commit()
 return current

def main():
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);a=p.parse_args();db=connect(a.db)
 from telegram import drain
 from local_credentials import load
 # Grace period restarts on every deploy, otherwise old worker heartbeats look like a stopped monitor.
 setmeta(db,'health_watch_started',time.time());db.commit()
 while True:
  try:
   check(db)
   from backup import automatic
   try:automatic(db,a.db);setmeta(db,'backup_error','');db.commit()
   except Exception:setmeta(db,'backup_error','Automatic backup failed; download a backup and check storage.');db.commit()
   if load():
    db.execute('BEGIN IMMEDIATE');drain(db,commit=False,system_only=True);db.commit()
   # A standby mirrors the primary's alerts from its Litestream replica every few minutes.
   try:
    import failover
    failover.maybe_sync_config(db)
   except Exception as error:
    db.rollback();__import__('logging').warning('standby alert sync failed: %s',str(error)[:200])
  except Exception:
   db.rollback()
  time.sleep(15)
if __name__=='__main__':main()
