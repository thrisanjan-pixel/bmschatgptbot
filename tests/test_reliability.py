import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from store import connect,setmeta,record_check,emit
import datetime as dt
from api_source import IST
import health_watch,watch_preferences as prefs,api_watch,row_watch,telegram

# Dates relative to today in IST so the tests do not rot.
D1=(dt.datetime.now(IST)+dt.timedelta(days=2)).strftime('%Y%m%d');D2=(dt.datetime.now(IST)+dt.timedelta(days=4)).strftime('%Y%m%d')

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
  prefs.save(self.db,{**prefs.defaults(),'movies':['Watched'],'dates':[D1],'enabled':True},create=True)
  prefs.save(self.db,{**prefs.defaults(),'movies':['Second'],'enabled':True},create=True)
  for name,movie,day,due in [('other-date','Watched',D2,980),('alert-date','Watched',D1,990),('undated','Watched','',991),('second','Second','',995)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':movie,'date':day}),due))
  self.db.commit();order=[]
  for _ in range(4):
   job=api_watch.next_job(self.db,1000);order.append(job['id']);self.db.execute('UPDATE jobs SET due=5000 WHERE id=?',(job['id'],));self.db.commit()
  # The undated listing is background: the alert's only date already has its own listing.
  self.assertEqual(order[:2],['alert-date','second']);self.assertEqual(set(order[2:]),{'other-date','undated'})
  self.assertEqual(api_watch.poll_interval(self.db,{'kind':'discovery','payload':json.dumps({'movie':'Watched','date':D2})}),120)
 def test_movie_with_many_listings_does_not_starve_another_alert_movie(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Big'],'enabled':True},create=True)
  prefs.save(self.db,{**prefs.defaults(),'movies':['Small'],'enabled':True},create=True)
  # Routine listings (already scheduled), so the older one for the other movie still goes first.
  for i in range(10):self.db.execute('INSERT INTO jobs(id,kind,url,payload,due,checked) VALUES(?,?,?,?,995,0)',('big'+str(i),'discovery','https://example.test/b'+str(i),json.dumps({'movie':'Big','date':''})))
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,due,checked) VALUES('small','discovery','https://example.test/s',?,990,500)",(json.dumps({'movie':'Small','date':''}),))
  self.db.execute("UPDATE jobs SET checked=900 WHERE id='big0'");setmeta(self.db,'listing_background_attempt',995);self.db.commit()
  self.assertEqual(api_watch.next_job(self.db,1000)['id'],'small')
 def test_screen_filter_keeps_discovery_for_new_cinemas_in_alert_queue(self):
  movie='Avengers: Doomsday'
  alert={**prefs.defaults(),'movies':[movie],'cinemas':['ALUC'],'screens':['DOLBY CINEMA'],'enabled':True,'urgent':True}
  prefs.save(self.db,alert,create=True)
  now=time.time()
  existing={'movie':movie,'event':'ET00439706','language':'English • 2D','format':'Infinity Vision Dolby Atmos',
            'venue_code':'AACN','date':D1,'categories':{}}
  self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',('existing',json.dumps(existing),now+3600))
  api_watch.schedule(self.db,existing['event'],'',movie)
  api_watch.schedule(self.db,existing['event'],D1,movie)
  setmeta(self.db,'listing_background_attempt',now)
  self.db.commit()
  job=api_watch.next_job(self.db,now)
  self.assertIsNotNone(job)
  self.assertEqual(json.loads(job['payload'])['date'],D1)
  self.assertTrue(api_watch.is_watched(json.loads(job['payload']),api_watch.wanted_dates(self.db,urgent=True)))
  at_allu={**existing,'venue_code':'ALUC'}
  self.assertFalse(prefs.matches(at_allu,alert))
  self.assertTrue(prefs.matches({**at_allu,'format':'DOLBY CINEMA'},alert))
 def test_version_language_and_format_still_filter_listing_priority(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'languages':['English'],'formats':['2D'],'enabled':True})
  for event,language in [('ET1','English • 2D'),('ET2','Telugu • 2D'),('ET3','English • 3D')]:
   self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(event,json.dumps({'event':event,'language':language,'format':''}),9e9))
  wanted=api_watch.wanted_dates(self.db)
  for event,expected in [('ET1',True),('ET2',False),('ET3',False),('ETNEW',True)]:
   with self.subTest(event=event):
    self.assertEqual(api_watch.is_watched({'movie':'Epic','event':event,'date':''},wanted),expected)
 def test_check_queue_lists_alert_checks_next_and_recent(self):
  import dashboard
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  now=time.time()
  for name,movie,due,checked in [('a','Epic',now-30,now-400),('b','Epic',now+500,now-100),('bg','Other',now-30,0)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due,checked) VALUES(?,?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':movie,'date':D1,'event':'ET1'}),due,checked))
  profile={'event':'ET1','language':'English • MS-Infinity Vsn 3d','format':'LASER 4K ATMOS'}
  self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',('version-example',json.dumps(profile),now+3600))
  record_check(self.db,'listings','b',now-100,now=now-100);self.db.commit()
  data=dashboard.check_queue(self.db)
  self.assertEqual([(i['kind'],i['movie']) for i in data['upcoming']],[('showtimes','Epic'),('showtimes','Epic')])
  self.assertEqual(data['upcoming'][0]['detail'],'Listing version · English · MS-Infinity Vsn 3d')
  self.assertEqual(data['upcoming'][0]['event'],'ET1')
  self.assertLess(data['upcoming'][0]['eta'],data['upcoming'][1]['eta'])
  self.assertEqual([(i['movie'],i['ok']) for i in data['recent']],[('Epic',True)])
 def test_stalled_check_warning_follows_minutes_and_off_switch(self):
  alert=prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  self.db.execute('UPDATE alerts SET created=0 WHERE id=?',(alert,))
  now=time.time();setmeta(self.db,'health_watch_started',now-3600);setmeta(self.db,'city_heartbeat',now);self.db.commit()
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,checked) VALUES('j','discovery','https://example.test/j',?,?)",(json.dumps({'movie':'Epic'}),now-1200))
  self.db.commit()
  stale=lambda:[k for k in health_watch.problems(self.db,now) if k.startswith('source:')]
  self.assertEqual(len(stale()),1)
  self.assertIn('over 15 minutes',health_watch.problems(self.db,now)[stale()[0]])
  setmeta(self.db,'stale_warning_minutes','30');self.db.commit();self.assertEqual(stale(),[])
  setmeta(self.db,'stale_warning_minutes','15');setmeta(self.db,'stale_warning_enabled','0');self.db.commit();self.assertEqual(stale(),[])
  setmeta(self.db,'stale_warning_minutes','7');setmeta(self.db,'stale_warning_enabled','1');self.db.commit()
  self.assertEqual(health_watch.stale_settings(self.db),{'enabled':True,'minutes':15})
 def test_undated_listing_is_background_once_alert_dates_have_listings(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'dates':[D1],'enabled':True},create=True)
  self.db.execute("INSERT INTO jobs(id,kind,url,payload) VALUES('d','discovery','https://example.test/d',?)",(json.dumps({'movie':'Epic','event':'E1','date':D1}),));self.db.commit()
  wanted=api_watch.wanted_dates(self.db)
  self.assertFalse(api_watch.is_watched({'movie':'Epic','event':'E1','date':''},wanted))
  self.assertTrue(api_watch.is_watched({'movie':'Epic','event':'E2','date':''},wanted))
  self.assertTrue(api_watch.is_watched({'movie':'Epic','event':'E1','date':D1},wanted))
 def test_most_overdue_alert_listing_goes_first(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  for name,due in [('recent',990),('oldest',100),('new',0)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':'Epic','event':name}),due))
  setmeta(self.db,'listing_background_attempt',995);self.db.commit()
  self.assertEqual([r['id'] for r in api_watch.listing_order(self.db,1000)],['oldest','new','recent'])
 def test_alert_reports_latest_listing_check_not_an_old_failure(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  for name,error in [('old','Source HTTP 403'),('fresh','')]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,checked,error) VALUES(?,?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':'Epic'}),900,error))
  record_check(self.db,'listings','old',100,'Source HTTP 403',now=100);record_check(self.db,'listings','fresh',900,now=900);self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['health']['error'],'')
  record_check(self.db,'listings','old',950,'Source HTTP 403',now=950);self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['health']['error'],'Source HTTP 403')
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
  self.assertEqual(api_watch.next_job(self.db,1051)['id'],'active')
  self.assertEqual(api_watch.next_job(self.db,1291)['id'],'background')

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
  self.assertIsNone(api_watch.next_job(self.db,1051))
  self.assertEqual(api_watch.next_job(self.db,1291)['id'],'background')

 def test_row_alert_does_not_promote_movie_discovery_to_listing_watch(self):
  from tests.test_rows import RowTests
  f=RowTests();f.setUp()
  try:
   f.configure()
   self.assertEqual(api_watch.watched_movies(f.db),[])
   job={'kind':'discovery','payload':json.dumps({'movie':'Movie'})}
   self.assertEqual(api_watch.poll_interval(f.db,job),120)
  finally:f.tearDown()

 def test_refusals_on_one_movie_leave_other_listings_due(self):
  from api_source import ReadError
  prefs.save(self.db,{**prefs.defaults(),'movies':['Denied','Other'],'enabled':True})
  api_watch.schedule(self.db,'ET1000','','Denied')
  api_watch.schedule(self.db,'ET1001','','Other')
  for i in range(3):
   key='denied'+str(i)
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(key,'discovery','https://example.test/'+key,json.dumps({'movie':'Denied'}),1))
   job=self.db.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
   api_watch.record_listing_failure(self.db,job,ReadError('Source HTTP 403',403,180),1000+i,1000+i)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),0)
  ready=[json.loads(r['payload'])['event'] for r in api_watch.listing_order(self.db,1002)]
  self.assertEqual(set(ready),{'ET1000','ET1001'})
  api_watch.record_listing_failure(self.db,job,ReadError('Source HTTP 429',429,600),1010,1010)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),1610)

 def test_repeated_403_across_movies_cools_listing_worker_until_a_retry(self):
  import dashboard
  from api_source import ReadError
  monitor=api_watch.Monitor(self.path)
  for i,movie in enumerate(('First','First','Second')):
   api_watch.schedule(self.db,f'ET{i+1000}','',movie)
  denied=ReadError('Source HTTP 403',403,180)
  jobs=self.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
  for job,now in zip(jobs,(1000,1060,1120)):
   api_watch.record_listing_failure(self.db,job,denied,now,now)
  self.db.commit()
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),1420)
  self.db.close()
  self.db=connect(self.path)
  prefs.save(self.db,{**prefs.defaults(),'movies':['First'],'enabled':True})
  with patch('api_watch.time.time',return_value=1180):
   self.assertGreaterEqual(dashboard.check_queue(self.db)['upcoming'][0]['eta'],1420)
  with patch('api_watch.get_json',return_value={}) as get,patch('api_watch.no_regional_shows',return_value=True),patch.object(monitor.stop,'wait'):
   with patch('api_watch.time.time',return_value=1419):
    monitor.check(self.db)
   get.assert_not_called()
   with patch('api_watch.time.time',return_value=1420):
    monitor.check(self.db)
   get.assert_called_once()
  self.assertEqual(json.loads(api_watch.meta(self.db,'listing_refused_movies','[]')),[])

 def test_one_403_after_cooldown_does_not_restart_the_shared_wait(self):
  from api_source import ReadError
  for event,movie in [('ET1000','First'),('ET1001','First'),('ET1002','Second')]:
   api_watch.schedule(self.db,event,'',movie)
  jobs=self.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
  denied=ReadError('Source HTTP 403',403,180)
  for job,now in zip(jobs,(1000,1060,1120)):
   api_watch.record_listing_failure(self.db,job,denied,now,now)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),1420)
  resumed=self.db.execute('SELECT * FROM jobs WHERE id=?',(jobs[0]['id'],)).fetchone()
  api_watch.record_listing_failure(self.db,resumed,denied,1420,1420)
  self.assertEqual(float(api_watch.meta(self.db,'source_cooldown','0')),1420)
  self.assertEqual(self.db.execute('SELECT due FROM jobs WHERE id=?',(resumed['id'],)).fetchone()[0],1720)

 def test_repeated_listing_identity_error_defers_only_the_bad_listing(self):
  import dashboard
  from api_source import ListingIdentityError,parse
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True,'urgent':True})
  for event in ('ET1000','ET1001'):
   api_watch.schedule(self.db,event,D1,'Epic')
  self.db.commit()
  jobs=self.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
  bad={'data':{'additionalData':{'eventCode':'ET1000','dateCode':D2},'showtimeWidgets':[]}}
  with self.assertRaises(ListingIdentityError) as mismatch:
   parse(bad,'ET1000',D1)
  self.assertEqual((mismatch.exception.event,mismatch.exception.day),('ET1000',D2))
  api_watch.record_listing_failure(self.db,jobs[0],mismatch.exception,1000,1000)
  failed=self.db.execute('SELECT * FROM jobs WHERE id=?',(jobs[0]['id'],)).fetchone()
  self.assertEqual(failed['due'],1060)
  api_watch.record_listing_failure(self.db,failed,mismatch.exception,1060,1060)
  expected=[('ET1001',D1,0),('ET1000',D1,1120)]
  self.assertEqual([(r['event'],r['date'],r['due']) for r in dashboard.listing_queue(self.db)],expected)
  self.assertEqual(api_watch.next_job(self.db,1120)['id'],jobs[1]['id'])

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
  self.assertEqual(row_watch.poll_policy(self.db,87401),(60,60))
  self.assertEqual(row_watch.poll_policy(self.db,86501,False),(60,0))

 def test_normal_seat_policy_is_conservative_without_a_previous_restriction(self):
  self.assertEqual(row_watch.poll_policy(self.db,1000),(60,60))

 def test_new_watched_listing_queues_behind_long_overdue_and_ahead_of_routine(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  for name,due in [('a',100),('b',200),('c',300),('fresh',990),('new',0)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':'Epic','event':name}),due))
  self.db.commit()
  self.assertEqual([r['id'] for r in api_watch.listing_order(self.db,1000)],['a','b','c','new','fresh'])
  setmeta(self.db,'listing_background_attempt',995);self.db.commit()  # background turn recently spent: same order
  self.assertEqual([r['id'] for r in api_watch.listing_order(self.db,1000)],['a','b','c','new','fresh'])
 def test_never_checked_background_listing_keeps_waiting_behind_overdue_and_watched(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  for name,movie,due in [('bg-new','Other',0),('bg-old','Other',100),('w','Epic',990)]:
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://example.test/'+name,json.dumps({'movie':movie,'event':name}),due))
  self.db.commit()
  self.assertEqual([r['id'] for r in api_watch.listing_order(self.db,1000)],['bg-old','w','bg-new'])
 def test_undated_listing_is_background_for_an_undated_alert_once_a_dated_listing_is_live(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Epic'],'enabled':True},create=True)
  undated={'movie':'Epic','event':'E1','date':''}
  self.assertTrue(api_watch.is_watched(undated,api_watch.wanted_dates(self.db)))  # no dated listing yet: bookings may not be open
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,expires) VALUES('d','discovery','https://example.test/d',?,?)",(json.dumps({'movie':'Epic','event':'E1','date':D1}),time.time()-10))
  self.db.commit()
  self.assertTrue(api_watch.is_watched(undated,api_watch.wanted_dates(self.db)))  # expired listing does not count
  self.db.execute("UPDATE jobs SET expires=? WHERE id='d'",(time.time()+3600,));self.db.commit()
  wanted=api_watch.wanted_dates(self.db)
  self.assertFalse(api_watch.is_watched(undated,wanted))
  self.assertTrue(api_watch.is_watched({'movie':'Epic','event':'E2','date':''},wanted))  # another event: still watched
  self.assertTrue(api_watch.is_watched({'movie':'Epic','event':'E1','date':D1},wanted))
 def test_transport_errors_retry_in_a_minute_but_http_statuses_keep_their_delay(self):
  from api_source import ReadError
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,due) VALUES('j','discovery','https://example.test/j',?,1)",(json.dumps({'movie':'M'}),));self.db.commit()
  job=lambda:self.db.execute("SELECT * FROM jobs WHERE id='j'").fetchone()
  due=lambda:self.db.execute("SELECT due FROM jobs WHERE id='j'").fetchone()[0]
  for error,expected in [(TimeoutError('x'),60),(ConnectionResetError('x'),60),(ReadError('Source connection or JSON error'),60),(ReadError('Source HTTP 500',500,300),300),(ReadError('Source HTTP 403',403,180),180)]:
   api_watch.record_listing_failure(self.db,job(),error,1000,1000);self.assertEqual(due(),1000+expected,error)
  api_watch.record_listing_failure(self.db,job(),ReadError('Source HTTP 429',429,600),1000,1000);self.assertEqual(due(),1600)
 def _check(self,job_id,kind,payload,due,error):
  monitor=api_watch.Monitor(self.path)
  self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(job_id,kind,'https://example.test/'+job_id,json.dumps(payload),due));self.db.commit()
  with patch('api_watch.get_json',side_effect=error),patch.object(monitor.stop,'wait'):monitor.check(self.db)
 def test_wait_on_a_background_pick_gives_its_turn_back(self):
  from http_source import SourceWait
  setmeta(self.db,'listing_background_attempt',123);self.db.commit()
  self._check('bg','discovery',{'movie':'Other'},100,SourceWait(time.time()+40))
  self.assertEqual(float(api_watch.meta(self.db,'listing_background_attempt')),123)
  prefs.save(self.db,{**prefs.defaults(),'movies':['Watched'],'enabled':True},create=True)
  self.db.execute("UPDATE jobs SET due=5000");self.db.commit()
  self._check('w','discovery',{'movie':'Watched'},100,SourceWait(time.time()+40))
  self.assertEqual(float(api_watch.meta(self.db,'listing_background_attempt')),123)
 def test_catalog_wait_delays_only_the_catalog_job(self):
  from http_source import SourceWait
  until=time.time()+500
  self._check('cat','catalog',{},100,SourceWait(until))
  self.assertEqual(self.db.execute("SELECT due FROM jobs WHERE id='cat'").fetchone()[0],until)
  self.assertEqual(float(api_watch.meta(self.db,'listing_budget_until','0')),0)
 def test_undated_no_show_flag_is_set_only_by_the_undated_listing(self):
  monitor=api_watch.Monitor(self.path)
  for job_id,day in (('dated',D1),('undated','')):
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,100)',(job_id,'discovery','https://example.test/'+job_id,json.dumps({'movie':'M','event':'E'+job_id,'date':day})))
  self.db.commit()
  with patch('api_watch.get_json',return_value={}),patch('api_watch.no_regional_shows',return_value=True),patch.object(monitor.stop,'wait'):
   for job_id in ('dated','undated'):
    setmeta(self.db,'listing_background_attempt',0);self.db.execute('UPDATE jobs SET due=5000');self.db.execute('UPDATE jobs SET due=100 WHERE id=?',(job_id,));self.db.commit();monitor.check(self.db)
  self.assertEqual(api_watch.meta(self.db,'upcoming_event:Eundated'),'1')
  self.assertEqual(api_watch.meta(self.db,'upcoming_event:Edated'),'')


class HealthScalingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'state.db'))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_row_alert_threshold_scales_and_waits_out_cooldown(self):
  now=100000.0;setmeta(self.db,'health_watch_started',1);setmeta(self.db,'city_heartbeat',now);setmeta(self.db,'row_reader_heartbeat',now)
  rules=[{'preferences':{**prefs.defaults(),'row_show':f's{i}','movies':['M']},'health':{'last_success':now-1000,'error':''},'created':0,'row_targets':1} for i in range(5)]
  with patch.object(prefs,'active',return_value=rules):
   self.assertEqual(health_watch.problems(self.db,now),{})
   rules[0]['health']['last_success']=now-3000;self.assertIn('source:s0',health_watch.problems(self.db,now))
   setmeta(self.db,'seat_cooldown_until',now+500);self.assertEqual(health_watch.problems(self.db,now),{})
