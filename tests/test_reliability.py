import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from store import connect,setmeta,record_check,emit
import health_watch,watch_preferences as prefs,api_watch,row_watch,telegram

class ReliabilityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
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
