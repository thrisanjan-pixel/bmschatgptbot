import copy,json,os,tempfile,time,unittest,subprocess,signal
import threading,urllib.request,urllib.parse
from http.server import ThreadingHTTPServer
from dashboard import handler
from pathlib import Path
from unittest.mock import patch,Mock
from store import connect,setmeta,meta
import row_watch
import watch_preferences as prefs
from tests.test_api_watch import response,DAY,EVENT
from api_source import parse

class RowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'s.db'))
  self.show=next(iter(parse(response(),EVENT,DAY)['shows'].values()));self.show['movie']='Movie'
  self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(self.show['id'],json.dumps(self.show),self.show['cutoff']));self.db.commit()
  row_watch.request(self.db,self.show['id'])
  self.result={'rows':[{'id':'Gold / A','category':'Gold','row':'A'},{'id':'Gold / B','category':'Gold','row':'B'}],'available':[{'row_id':'Gold / B','seat':'1'}],'url':self.show['url']}
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def configure(self):
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'categories':['Gold'],'row_show':self.show['id'],'rows':['Gold / A'],'enabled':True})
 def test_new_showtime_jumps_queue_and_reports_source_wait(self):
  now=time.time()
  self.db.execute("INSERT INTO row_layouts(show_id,payload,checked,due) VALUES('alert-show','{}',?,?)",(now-900,now-800))
  self.db.execute('UPDATE row_layouts SET due=?,manual=0 WHERE show_id=?',(now-10,self.show['id']));self.db.commit()
  self.assertEqual(row_watch.next_job(self.db,now)['show_id'],self.show['id'])
  setmeta(self.db,'seat_budget_until',now+300);self.db.commit()
  self.assertIsNone(row_watch.next_job(self.db,now))
  self.assertEqual(row_watch.get(self.db,self.show['id'])['next_read'],now+300)
 def test_overdue_alert_showtime_beats_new_showtimes(self):
  self.configure();now=time.time()
  self.db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now-300,self.show['id']))
  self.db.execute("INSERT INTO row_layouts(show_id,payload,due) VALUES('browsed','{}',?)",(now-10,));self.db.commit()
  self.assertEqual(row_watch.next_job(self.db,now)['show_id'],self.show['id'])
  self.db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now-60,self.show['id']));self.db.commit()
  self.assertEqual(row_watch.next_job(self.db,now)['show_id'],'browsed')
 def test_queue_lists_reads_in_order_and_remove_skips_alert_showtimes(self):
  self.configure();now=time.time()
  self.db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now-300,self.show['id']))
  self.db.execute("INSERT INTO row_layouts(show_id,payload,due) VALUES('browsed','{}',?)",(now-10,));self.db.commit()
  items=row_watch.queue(self.db,now)
  self.assertEqual([q['show_id'] for q in items],[self.show['id'],'browsed'])
  self.assertTrue(items[0]['alert']);self.assertEqual(items[1]['eta'],items[0]['eta']+60)
  with self.assertRaises(ValueError):row_watch.dequeue(self.db,self.show['id'])
  row_watch.dequeue(self.db,'browsed')
  self.assertEqual([q['show_id'] for q in row_watch.queue(self.db,now)],[self.show['id']])
 def test_load_seat_rows_runs_at_once_outside_the_alert_queue(self):
  now=time.time();setmeta(self.db,'seat_next_request_at',now+50);self.db.commit()
  self.assertIsNone(row_watch.next_job(self.db,now+1))
  self.assertEqual(row_watch.manual_job(self.db,now+1)['show_id'],self.show['id'])
  self.assertLess(row_watch.get(self.db,self.show['id'])['next_read'],now+5)
  with patch.dict(os.environ,{'BMS_PUBLIC':'1'}),patch('cloud_row_reader.read',return_value=copy.deepcopy(self.result)) as read:
   row_watch.step(self.db)
  self.assertTrue(read.call_args.kwargs['manual'])
  self.assertEqual(float(meta(self.db,'seat_next_request_at')),now+50)
  self.assertEqual(self.db.execute('SELECT manual,due FROM row_layouts WHERE show_id=?',(self.show['id'],)).fetchone()[:],(0,0))
  setmeta(self.db,'seat_cooldown_until',time.time()+600);self.db.commit()
  self.db.execute('UPDATE row_layouts SET due=?,manual=1',(time.time(),));self.db.commit()
  self.assertIsNone(row_watch.manual_job(self.db,time.time()+1))
 def test_retry_now_clears_saved_seat_waits(self):
  now=time.time()
  for key in ('seat_cooldown_until','seat_budget_until','seat_next_request_at'):setmeta(self.db,key,now+5000)
  self.db.execute('UPDATE row_layouts SET due=? WHERE show_id=?',(now+5000,self.show['id']));self.db.commit()
  row_watch.clear_source_wait(self.db)
  self.assertEqual(row_watch.source_ready_at(self.db),0)
  self.assertEqual(row_watch.next_job(self.db,time.time()+1)['show_id'],self.show['id'])
 def test_specific_row_release_and_silent_baseline(self):
  self.configure();self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
  changed=copy.deepcopy(self.result);changed['available'].append({'row_id':'Gold / B','seat':'2'})
  row_watch.ingest(self.db,self.show,changed);self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
  changed['available'].append({'row_id':'Gold / A','seat':'3'})
  row_watch.ingest(self.db,self.show,changed)
  self.assertIn('Gold / A: 3',self.db.execute('select text from outbox').fetchone()[0])
  row_watch.ingest(self.db,self.show,changed);self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],1)
 def test_alert_text_orders_seats_numerically_and_formats_date(self):
  self.configure()
  changed=copy.deepcopy(self.result);changed['available']+=[{'row_id':'Gold / A','seat':s} for s in ('25','2','1','3')]
  row_watch.ingest(self.db,self.show,changed)
  text=self.db.execute('select text from outbox').fetchone()[0]
  self.assertIn('Gold / A: 1, 2, 3, 25',text);self.assertIn(row_watch.show_date(self.show),text)
  self.assertNotIn('📅 '+self.show['date'],text);self.assertTrue(text.rstrip().endswith(self.show['url']))
  self.assertIn('4 seats now available',text);self.assertIn('(4 seats · ₹200 each)',text)
 def test_seat_read_marks_only_seats_opened_since_last_read(self):
  first=copy.deepcopy(self.result);first['seats']=[{'row_id':'Gold / B','seat':'1','available':True}]
  row_watch.ingest(self.db,self.show,copy.deepcopy(first))
  self.assertEqual(row_watch.get(self.db,self.show['id'])['data']['new'],[])
  first['available'].append({'row_id':'Gold / A','seat':'3'});row_watch.ingest(self.db,self.show,first)
  self.assertEqual(row_watch.get(self.db,self.show['id'])['data']['new'],[{'row_id':'Gold / A','seat':'3'}])
 def test_listing_alert_shows_seat_type_price(self):
  self.assertEqual(prefs.with_price(self.show,'gold'),'gold (₹200)');self.assertEqual(prefs.with_price(self.show,'Silver'),'Silver')
 def test_invalid_rows_and_wrong_show_filters_rejected(self):
  self.configure();p=prefs.read(self.db)
  for updates in ({'rows':['Gold / Z']},{'movies':['Other']},{'row_show':'missing'}):
   with self.assertRaises(ValueError):prefs.save(self.db,{**p,**updates})
 def test_pause_and_remember_unavailable_rows(self):
  self.configure();setmeta(self.db,'notifications_paused','1');self.db.commit()
  changed=copy.deepcopy(self.result);changed['available']=[{'row_id':'Gold / A','seat':'3'}];changed['rows']=[]
  row_watch.ingest(self.db,self.show,changed)
  self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
  self.assertEqual(len(row_watch.get(self.db,self.show['id'])['data']['rows']),2)
 def test_expired_or_unknown_show_request_rejected(self):
  with self.assertRaises(ValueError):row_watch.request(self.db,'invalid')
 def test_repeated_load_does_not_bypass_failure_backoff(self):
  due=time.time()+300
  self.db.execute("UPDATE row_layouts SET payload='{}',checked=0,error='Seat page unavailable (HTTP 403)',due=? WHERE show_id=?",(due,self.show['id']));self.db.commit()
  row_watch.request(self.db,self.show['id'])
  saved=self.db.execute('SELECT due,error FROM row_layouts WHERE show_id=?',(self.show['id'],)).fetchone()
  self.assertEqual(saved['error'],'Seat page unavailable (HTTP 403)');self.assertGreater(saved['due'],time.time()+295)
 def test_cached_layout_remains_usable_during_cloud_403_cooldown(self):
  self.configure();due=time.time()+300
  self.db.execute("UPDATE row_layouts SET error='Seat page unavailable (HTTP 403)',due=? WHERE show_id=?",(due,self.show['id']));self.db.commit()
  saved=prefs.read(self.db)
  alert_id=prefs.save(self.db,{**saved,'enabled':True})
  self.assertEqual(alert_id,prefs.read(self.db) and prefs.rules(self.db)[0]['id'])
  self.assertEqual(len(row_watch.get(self.db,self.show['id'])['data']['rows']),2)
 def test_expired_row_watch_can_always_be_paused(self):
  self.configure();p=prefs.read(self.db)
  self.db.execute('UPDATE sessions SET cutoff=0');self.db.commit()
  prefs.save(self.db,{**p,'enabled':False})
  self.assertFalse(prefs.read(self.db)['enabled']);self.assertEqual(meta(self.db,'notifications_paused'),'1')
 def test_row_watch_does_not_require_category_or_new_show_alerts(self):
  self.configure();p=prefs.read(self.db)
  prefs.save(self.db,{**p,'new_shows':False,'availability':False})
  self.assertTrue(prefs.read(self.db)['enabled'])
 def test_get_row_status_route(self):
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(Path(self.tmp.name)/'s.db')))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/rows?show='+urllib.parse.quote(self.show['id'])) as response:
    data=json.load(response)
   self.assertEqual(data['show_id'],self.show['id'])
   self.assertEqual(data['data']['rows'][0]['id'],'Gold / A')
  finally:server.shutdown();server.server_close();thread.join()
 def test_timed_out_reader_kills_the_browser_process_group(self):
  process=Mock(pid=321)
  process.communicate.side_effect=[subprocess.TimeoutExpired('reader',1),('','')]
  with patch('row_watch.subprocess.Popen',return_value=process) as launch,patch('row_watch.os.killpg') as kill:
   with self.assertRaisesRegex(ValueError,'timed out'):row_watch.run_reader(self.show,timeout=1)
  self.assertTrue(launch.call_args.kwargs['start_new_session'])
  kill.assert_called_once_with(321,signal.SIGKILL)
  self.assertEqual(process.communicate.call_count,2)

