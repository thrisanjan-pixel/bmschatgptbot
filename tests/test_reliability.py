import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from store import connect,setmeta,record_check,emit
import health_watch,watch_preferences as prefs,api_watch,row_watch,telegram

class ReliabilityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_api_mode_counts_cinemas_from_live_showtimes(self):
  from store import coverage
  future=time.time()+3600
  for sid,venue,cutoff in (('a','AMBH',future),('b','AMBH',future),('c','PVR',future),('d','OLD',time.time()-60)):
   self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(sid,json.dumps({'venue_code':venue}),cutoff))
  self.db.execute("INSERT INTO sessions VALUES('e','not json',?,1)",(future,))
  self.assertEqual(coverage(self.db)['venues'],2)
 def test_timings_and_failures_survive_restart_without_erasing_success(self):
  record_check(self.db,'rows','show',98,now=100);record_check(self.db,'rows','show',128,now=130)
  record_check(self.db,'rows','show',159,'HTTP 403',now=160);self.db.commit();self.db.close();self.db=connect(self.path)
  row=self.db.execute('SELECT * FROM check_stats').fetchone()
  self.assertEqual((row['attempts'],row['successes'],row['failures']),(3,2,1));self.assertEqual(row['last_success'],130);self.assertEqual(row['last_interval'],30)
 def test_warning_dedup_and_recovery_survive_restart(self):
  setmeta(self.db,'health_watch_started',1000);self.db.commit()
  health_watch.check(self.db,1400);health_watch.check(self.db,1415)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
  self.db.close();self.db=connect(self.path);health_watch.check(self.db,1430)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
  setmeta(self.db,'city_heartbeat',1440);self.db.commit();health_watch.check(self.db,1440)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
 def test_active_movie_priority_and_background_fairness(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Watched'],'enabled':True})
  for name,movie,due in [('background','Other',990),('active','Watched',995)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'movie','https://example.test/'+name,json.dumps({'movie':movie}),due))
  self.db.commit();self.assertEqual(api_watch.next_job(self.db,1000)['id'],'active')
  self.db.execute("UPDATE jobs SET due=600 WHERE id='background'");self.db.commit();self.assertEqual(api_watch.next_job(self.db,1000)['id'],'background')
 def test_dates_no_alert_asks_for_do_not_crowd_out_other_alerts(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Watched'],'dates':['20261106'],'enabled':True},create=True)
  prefs.save(self.db,{**prefs.defaults(),'movies':['Second'],'enabled':True},create=True)
  for name,movie,day,due in [('other-date','Watched','20261110',980),('alert-date','Watched','20261106',990),('undated','Watched','',991),('second','Second','',995)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':movie,'date':day}),due))
  self.db.commit();order=[]
  for _ in range(4):
   job=api_watch.next_job(self.db,1000);order.append(job['id']);self.db.execute('UPDATE jobs SET due=5000 WHERE id=?',(job['id'],));self.db.commit()
  self.assertEqual(order[:3],['alert-date','undated','second'])
  self.assertEqual(api_watch.poll_interval(self.db,{'kind':'discovery','payload':json.dumps({'movie':'Watched','date':'20261110'})}),120)
 def test_duplicate_row_rules_share_one_queued_read(self):
  from tests.test_rows import RowTests
  fixture=RowTests();fixture.setUp()
  try:
   fixture.configure();p=prefs.read(fixture.db);prefs.save(fixture.db,p,create=True)
   fixture.db.execute('UPDATE row_layouts SET checked=1,due=0');fixture.db.commit();row_watch.schedule_active(fixture.db,time.time())
   self.assertEqual(fixture.db.execute('SELECT count(*) FROM row_layouts WHERE due>0').fetchone()[0],1)
  finally:fixture.tearDown()
 def test_health_delivery_retry_survives_restart_and_ignores_normal_messages(self):
  emit(self.db,'normal','normal',alert_id='normal');emit(self.db,'health','health',alert_id='__health__');self.db.commit()
  with patch('telegram.send',side_effect=telegram.DeliveryError(503,30)):
   telegram.drain(self.db,system_only=True)
  self.db.close();self.db=connect(self.path)
  self.assertEqual(self.db.execute("SELECT attempts FROM outbox WHERE alert_id='__health__'").fetchone()[0],1)
  self.db.execute('UPDATE outbox SET next_try=0');self.db.commit()
  with patch('telegram.send') as send:telegram.drain(self.db,system_only=True);self.assertEqual(send.call_args.args[0],'health')
  self.assertEqual(self.db.execute("SELECT delivered FROM outbox WHERE alert_id='normal'").fetchone()[0],0)
 def test_hosting_expiry_unknown_or_approaching_is_honest(self):
  with patch.dict(os.environ,{'SCREENWATCH_HOSTING_ENDS_AT':''}):self.assertFalse(health_watch.hosting_status(100)['allowance_connected'])
  with patch.dict(os.environ,{'SCREENWATCH_HOSTING_ENDS_AT':'1970-01-02T00:00:00+00:00'}):self.assertIn('within 3 days',health_watch.hosting_status(100)['warning'])

 def test_external_health_endpoint_detects_monitor_failure(self):
  import threading,urllib.request,urllib.error
  from http.server import ThreadingHTTPServer
  from dashboard import handler
  now=time.time();setmeta(self.db,'city_heartbeat',now);setmeta(self.db,'health_watch_heartbeat',now);self.db.commit()
  server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.path));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   url=f'http://127.0.0.1:{server.server_port}'
   with urllib.request.urlopen(url+'/readyz') as response:self.assertEqual(response.status,200)
   setmeta(self.db,'city_heartbeat',now-180);self.db.commit()
   with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(url+'/readyz')
   self.assertEqual(failure.exception.code,503);failure.exception.close()
   with urllib.request.urlopen(url+'/healthz') as response:self.assertEqual(response.status,200)
  finally:server.shutdown();server.server_close();thread.join()

 def test_recent_background_attempt_keeps_overdue_discovery_from_starving_alert(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Watched'],'enabled':True})
  for name,movie,due in [('background','Other',100),('active','Watched',995)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'movie','https://example.test/'+name,json.dumps({'movie':movie}),due))
  setmeta(self.db,'listing_background_attempt',990);self.db.commit()
  self.assertEqual(api_watch.next_job(self.db,1000)['id'],'active')
  self.assertEqual(api_watch.next_job(self.db,1051)['id'],'background')

 def test_live_outage_is_included_in_report_without_rewriting_past_average(self):
  alert=prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'enabled':True})
  self.db.execute('UPDATE alerts SET created=1 WHERE id=?',(alert,))
  self.db.execute('INSERT INTO jobs(id,kind,payload,checked,due,error) VALUES(?,?,?,?,?,?)',('job','movie',json.dumps({'movie':'Movie'}),100,300,'Source HTTP 403'))
  record_check(self.db,'listings','job',70,now=70);record_check(self.db,'listings','job',100,now=100)
  setmeta(self.db,'source_cooldown',2000);self.db.commit()
  with patch('watch_preferences.time.time',return_value=1000):rule=prefs.rules(self.db)[0]
  self.assertEqual(rule['report']['average_gap'],30)
  self.assertEqual(rule['report']['ongoing_gap'],900)
  self.assertEqual(rule['report']['longest_gap_including_current'],900)
  self.assertEqual(rule['health']['next_check'],2000)

 def test_shared_seat_retry_applies_to_every_row_alert_status(self):
  from tests.test_rows import RowTests
  f=RowTests();f.setUp()
  try:
   f.configure();now=time.time();setmeta(f.db,'seat_cooldown_until',now+36000);f.db.commit()
   rule=prefs.rules(f.db)[0]
   self.assertEqual(rule['health']['next_check'],now+36000)
   self.assertEqual(rule['health']['retry_until'],now+36000)
  finally:f.tearDown()

 def test_background_request_budget_applies_even_when_no_alert_is_due(self):
  self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',('background','movie','https://example.test/other',json.dumps({'movie':'Other'}),100))
  setmeta(self.db,'listing_background_attempt',990);self.db.commit()
  self.assertIsNone(api_watch.next_job(self.db,1000))
  self.assertEqual(api_watch.next_job(self.db,1051)['id'],'background')

 def test_row_alert_does_not_promote_movie_discovery_to_listing_watch(self):
  from tests.test_rows import RowTests
  f=RowTests();f.setUp()
  try:
   f.configure()
   self.assertEqual(api_watch.watched_movies(f.db),[])
   job={'kind':'discovery','payload':json.dumps({'movie':'Movie'})}
   self.assertEqual(api_watch.poll_interval(f.db,job),120)
  finally:f.tearDown()

 def test_three_denied_jobs_cannot_pause_other_movies(self):
  from api_source import ReadError
  prefs.save(self.db,{**prefs.defaults(),'movies':['Denied','Other'],'enabled':True})
  for i in range(3):
   key='denied'+str(i)
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(key,'movie','https://example.test/'+key,json.dumps({'movie':'Denied'}),1))
   job=self.db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
   api_watch.record_listing_failure(self.db,job,ReadError('Source HTTP 403',403,180),1000+i,1000+i)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),0)
  api_watch.record_listing_failure(self.db,job,ReadError('Source HTTP 429',429,600),1010,1010)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),1610)

 def test_refusal_diagnostics_never_include_cookies_or_body(self):
  from api_source import get_json,ReadError,retry_delay
  import io,urllib.error
  error=urllib.error.HTTPError('https://in.bookmyshow.com/test',429,'limited',{'Retry-After':' 600 ','Set-Cookie':'private','Server':'test'},io.BytesIO(b'private body'))
  with patch('api_source.urllib.request.urlopen',side_effect=error):
   with self.assertRaises(ReadError) as failure:get_json('https://in.bookmyshow.com/test')
  self.assertEqual(failure.exception.retry,600)
  self.assertEqual(failure.exception.diagnostic,{'status':429,'headers':{'Retry-After':' 600 ','Server':'test'}})

 def test_long_seat_restriction_still_has_recovery_window_after_expiry(self):
  setmeta(self.db,'seat_last_rate_limit',100)
  setmeta(self.db,'seat_cooldown_until',86500);self.db.commit()
  self.assertEqual(row_watch.poll_policy(self.db,86501),(120,60))
  self.assertEqual(row_watch.poll_policy(self.db,87399),(120,60))
  self.assertEqual(row_watch.poll_policy(self.db,87401),(60,30))
  self.assertEqual(row_watch.poll_policy(self.db,86501,False),(60,0))

 def test_normal_seat_policy_is_conservative_without_a_previous_restriction(self):
  self.assertEqual(row_watch.poll_policy(self.db,1000),(60,30))


class HealthScalingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'state.db'))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_row_alert_threshold_scales_and_waits_out_cooldown(self):
  now=100000.0;setmeta(self.db,'health_watch_started',1);setmeta(self.db,'city_heartbeat',now);setmeta(self.db,'row_reader_heartbeat',now)
  rules=[{'preferences':{**prefs.defaults(),'row_show':f's{i}','movies':['M']},'health':{'last_success':now-1000,'error':''},'created':0} for i in range(5)]
  with patch.object(prefs,'active',return_value=rules):
   self.assertEqual(health_watch.problems(self.db,now),{})
   rules[0]['health']['last_success']=now-3000;self.assertIn('source:s0',health_watch.problems(self.db,now))
   setmeta(self.db,'seat_cooldown_until',now+500);self.assertEqual(health_watch.problems(self.db,now),{})
