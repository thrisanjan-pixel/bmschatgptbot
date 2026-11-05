"""Worker processes keep running and keep delivering through upstream and storage faults."""
import copy,http.client,io,json,os,sqlite3,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from store import connect,emit,meta,setmeta
import api_watch,backup,cloud_row_reader,row_watch,seat_map,telegram
import watch_preferences as prefs
from api_source import parse
from tests.test_api_watch import response,DAY,EVENT
from tests.test_rows import RowTests

ENV={'TELEGRAM_BOT_TOKEN':'﻿123:test-only \n','TELEGRAM_CHAT_ID':'﻿ 4242\r\n'}

class Calls:
 """Fake Telegram: records each method and fails the ones listed in errors."""
 def __init__(self,errors=None):self.calls=[];self.errors=errors or {}
 def __call__(self,method,data,photo=None):
  self.calls.append((method,dict(data),photo))
  if method in self.errors:raise self.errors[method]
  return {}

class TelegramCredentialTests(unittest.TestCase):
 def test_bom_and_whitespace_are_stripped_from_token_and_chat(self):
  with patch.dict(os.environ,ENV):
   self.assertEqual(telegram.credential('TELEGRAM_BOT_TOKEN'),'123:test-only');self.assertTrue(telegram.configured())
   with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"ok":true,"result":{}}')) as urlopen:telegram.send('hello')
   request=urlopen.call_args.args[0]
   self.assertTrue(request.full_url.endswith('/bot123:test-only/sendMessage'))
   self.assertEqual(json.loads(request.data)['chat_id'],'4242')
 def test_bom_only_values_are_not_configured(self):
  from local_credentials import load
  with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'﻿','TELEGRAM_CHAT_ID':' ﻿ '}),patch('local_credentials.location',return_value=Path('missing-telegram.json')):
   self.assertFalse(telegram.configured());self.assertFalse(load())
   with self.assertRaises(telegram.DeliveryError):telegram.send('hello')

class TelegramDeliveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'s.db'))
  self.env=patch.dict(os.environ,ENV);self.env.start()
 def tearDown(self):self.env.stop();self.db.close();self.tmp.cleanup()
 def test_photo_failure_falls_back_to_text_with_exact_link(self):
  text='🎟 Seats now available\n\nhttps://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00000001/TEST/1/20261110'
  fake=Calls({'sendPhoto':telegram.DeliveryError(400,reason='Bad Request: IMAGE_PROCESS_FAILED')})
  with patch('telegram.request',fake):telegram.send(text,b'png')
  self.assertEqual([c[0] for c in fake.calls],['sendPhoto','sendMessage']);self.assertEqual(fake.calls[1][1]['text'],text)
 def test_photo_rate_limit_is_honoured_not_bypassed(self):
  fake=Calls({'sendPhoto':telegram.DeliveryError(429,120)})
  with patch('telegram.request',fake),self.assertRaises(telegram.DeliveryError) as raised:telegram.send('short',b'png')
  self.assertEqual(raised.exception.retry,120);self.assertEqual([c[0] for c in fake.calls],['sendPhoto'])
 def test_long_text_is_sent_once_even_if_map_fails(self):
  fake=Calls({'sendPhoto':telegram.DeliveryError(400)})
  with patch('telegram.request',fake):telegram.send('x'*1200+'\nhttps://in.bookmyshow.com/x',b'png')
  self.assertEqual([c[0] for c in fake.calls],['sendMessage','sendPhoto'])
 def test_unexpected_transport_error_is_retried_and_logged_safely(self):
  emit(self.db,'a','alert',alert_id='rule');self.db.commit()
  with patch('telegram.send',side_effect=http.client.IncompleteRead(b'')),self.assertLogs(level='WARNING') as logs:
   self.assertIsNone(telegram.drain(self.db))
  row=self.db.execute('SELECT attempts,next_try,delivered FROM outbox').fetchone()
  self.assertEqual((row[0],row[2]),(1,0));self.assertGreater(row[1],time.time())
  output='\n'.join(logs.output);self.assertIn('HTTP 0',output);self.assertIn('retry in 30s',output)
  for secret in ('123:test-only','4242','api.telegram.org'):self.assertNotIn(secret,output)
 def test_malformed_error_parameters_become_delivery_errors(self):
  with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"ok":false,"error_code":500,"parameters":null}')):
   with self.assertRaises(telegram.DeliveryError) as raised:telegram.request('sendMessage',{})
  self.assertEqual(raised.exception.code,500)
 def test_message_rejected_repeatedly_stops_blocking_its_alert(self):
  emit(self.db,'bad','bad',alert_id='rule');emit(self.db,'cfg','cfg',alert_id='other');self.db.commit()
  self.db.execute('UPDATE outbox SET attempts=4');self.db.commit()
  with patch('telegram.send',side_effect=telegram.DeliveryError(400,reason='Bad Request: message text is empty')):telegram.drain(self.db)
  self.assertIsNone(self.db.execute("SELECT 1 FROM outbox WHERE id=(SELECT id FROM suppressed_outbox)").fetchone())
  self.assertEqual(self.db.execute('SELECT attempts FROM suppressed_outbox').fetchone()[0],5)
  self.db.execute('UPDATE outbox SET next_try=0');self.db.commit()
  with patch('telegram.send',side_effect=telegram.DeliveryError(400,reason='Bad Request: chat not found')):telegram.drain(self.db)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox WHERE delivered=0').fetchone()[0],1)

class RowAlertResilienceTests(RowTests):
 def test_long_seat_list_keeps_booking_link(self):
  self.configure();changed=copy.deepcopy(self.result)
  changed['available']+=[{'row_id':'Gold / A','seat':str(n)} for n in range(1,400)]
  row_watch.ingest(self.db,self.show,changed)
  text=self.db.execute('SELECT text FROM outbox').fetchone()[0]
  self.assertLessEqual(len(text),950);self.assertTrue(text.endswith('\n\n'+self.show['url']))
 def test_seat_map_failure_still_sends_text_alert(self):
  self.configure();changed=copy.deepcopy(self.result);changed['available'].append({'row_id':'Gold / A','seat':'3'})
  with patch('seat_map.render',side_effect=UnicodeEncodeError('latin-1','x',0,1,'font')):row_watch.ingest(self.db,self.show,changed)
  row=self.db.execute('SELECT text,photo FROM outbox').fetchone()
  self.assertIn('Gold / A: 3',row[0]);self.assertIsNone(row[1])
 def test_failed_read_discards_partial_alert_state(self):
  self.configure();row_watch.ingest(self.db,self.show,copy.deepcopy(self.result));before=self.db.execute('SELECT available FROM row_alert_state').fetchone()[0]
  changed=copy.deepcopy(self.result);changed['available'].append({'row_id':'Gold / A','seat':'3'})
  def partial(db,show,result):
   db.execute('UPDATE row_alert_state SET available=?',('[]',));raise ValueError('boom')
  self.db.execute('UPDATE row_layouts SET due=1,checked=0');self.db.commit()
  with patch.dict(os.environ,{'BMS_PUBLIC':'1'}),patch('cloud_row_reader.read',return_value=changed),patch('row_watch.ingest',partial):row_watch.step(self.db)
  self.assertEqual(self.db.execute('SELECT available FROM row_alert_state').fetchone()[0],before)
  self.assertEqual(self.db.execute('SELECT error FROM row_layouts').fetchone()[0],'boom')
 def test_main_loop_survives_locked_database(self):
  with patch.dict(os.environ,{'BMS_ROWS_ENABLED':'1'}),patch('sys.argv',['row_watch.py','--db','unused']),patch('time.sleep'),       patch('row_watch.connect'),patch('row_watch.schema'),\
       patch('row_watch.step',side_effect=[sqlite3.OperationalError('database is locked'),KeyboardInterrupt]) as step:
   with self.assertRaises(KeyboardInterrupt):row_watch.main()
  self.assertEqual(step.call_count,2)

class MonitorResilienceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'s.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_listing_worker_survives_locked_database(self):
  monitor=api_watch.Monitor(self.path)
  def check(db):
   if check.calls:monitor.stop.set();return
   check.calls+=1;raise sqlite3.OperationalError('database is locked')
  check.calls=0
  with patch.object(monitor,'check',side_effect=check),patch.object(monitor.stop,'wait'):monitor.worker()
  self.assertTrue(monitor.stop.is_set())
 def test_bad_initial_availability_does_not_block_delivery(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'enabled':True});emit(self.db,'queued','queued',alert_id='x');self.db.commit()
  monitor=api_watch.Monitor(self.path)
  with patch('api_watch.load',return_value=True),patch('watch_preferences.prime_availability',side_effect=KeyError('time')),\
       patch('telegram.send') as send,patch.object(monitor.stop,'wait'):monitor.tick(self.db)
  send.assert_called_once()
 def test_null_upstream_labels_become_text(self):
  data=response();item=data['data']['showtimeWidgets'][0]['data'][0]['data'][0]
  item['additionalData']['venueName']=None;item['showtimes'][0]['title']=None
  item['showtimes'][0]['additionalData']['categories'][0]['priceDesc']=None
  show=next(iter(parse(data,EVENT,DAY)['shows'].values()))
  self.assertEqual((show['venue'],show['time'],show['format'],show['categories']['1']['name']),('TEST','','','1'))
 def test_stored_show_without_time_or_valid_link_is_skipped(self):
  show=next(iter(parse(response(status='1'),EVENT,DAY)['shows'].values()));show['movie']='Movie'
  bad={**show,'id':'bad','time':None,'venue_code':'bad code'}
  for item in (show,bad):self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(item['id'],json.dumps(item),item['cutoff']))
  self.db.commit()
  self.assertFalse(prefs.matches(bad,{**prefs.defaults(),'time_from':'10:00'}))
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'enabled':True});prefs.prime_availability(self.db)
  text=self.db.execute('SELECT text FROM outbox').fetchone()[0]
  self.assertIn('/seat-layout/'+EVENT+'/TEST/',text);self.assertNotIn('bad code',text)

class StorageGrowthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'s.db'));row_watch.schema(self.db)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_expired_showtimes_jobs_and_snapshots_are_pruned(self):
  now=time.time();old=now-5*86400
  for sid,cutoff in [('old',old),('kept',old),('future',now+86400)]:
   self.db.execute('INSERT INTO sessions VALUES(?,?,?,0)',(sid,'{}',cutoff))
   self.db.execute("INSERT INTO row_layouts(show_id,payload) VALUES(?,'{}')",(sid,))
  prefs.ensure(self.db);self.db.execute('INSERT INTO alerts VALUES(?,?,?,?,0,0)',('r',json.dumps({'row_show':'kept','rows':['A']}),now,now))
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,expires) VALUES('j','discovery','u','{}',?)",(old,))
  self.db.execute("INSERT INTO jobs(id,kind,url,payload,expires) VALUES('k','discovery','v','{}',0)")
  setmeta(self.db,'waiting_show:j','1');setmeta(self.db,'revision:api:ET1:20000101','3')
  self.db.execute("INSERT INTO snapshots VALUES('api:ET1:20000101','{}')");self.db.execute("INSERT INTO snapshots VALUES('api:ET1:29990101','{}')")
  backup.cleanup_history(self.db,now)
  self.assertEqual({r[0] for r in self.db.execute('SELECT id FROM sessions')},{'kept','future'})
  self.assertEqual({r[0] for r in self.db.execute('SELECT show_id FROM row_layouts')},{'kept','future'})
  self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM jobs')],['k'])
  self.assertEqual([r[0] for r in self.db.execute('SELECT id FROM snapshots')],['api:ET1:29990101'])
  self.assertEqual((meta(self.db,'waiting_show:j'),meta(self.db,'revision:api:ET1:20000101')),('',''))

class SeatReaderTests(unittest.TestCase):
 def test_non_object_seat_response_is_a_clean_error(self):
  show={'event':'ET00000001','venue_code':'TEST','session':'1','date':'20261110','categories':{}}
  with patch('cloud_row_reader.open_request',return_value=io.BytesIO(b'[]')):
   with self.assertRaisesRegex(ValueError,'Seat source response changed'):cloud_row_reader.read(show)
 def test_area_cache_drops_stale_entries(self):
  cloud_row_reader._area_cache.clear();cloud_row_reader._area_cache[('old',)]=(time.time()-cloud_row_reader.AREA_TTL-100,{})
  show={'event':'ET00000001','venue_code':'TEST','date':'20261110','session':'1','categories':{'1':{'name':'Gold'}}}
  data={'data':{'eventData':{'eventCode':'ET00000001'},'venue':{'venueCode':'TEST'},'showTimes':[{'sessionId':'1','categories':[{'priceCode':'1','areaCatCode':'7'}]}]}}
  with patch('cloud_row_reader.open_request',return_value=io.BytesIO(json.dumps(data).encode())):
   self.assertEqual(cloud_row_reader._areas(show,5),{'7':'Gold'})
  self.assertNotIn(('old',),cloud_row_reader._area_cache);cloud_row_reader._area_cache.clear()
 def test_seat_map_tolerates_missing_date(self):
  png=seat_map.render({'date':None,'movie':'M'},{'rows':[{'id':'Gold / A'}],'seats':[{'row_id':'Gold / A','seat':'1','available':True}]})
  self.assertTrue(png.startswith(b'\x89PNG'))

if __name__=='__main__':unittest.main()
