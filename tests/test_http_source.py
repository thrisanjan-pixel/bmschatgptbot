import io,os,tempfile,time,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from email.message import Message
from store import connect
import http_source as source

class SourceBudgetTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_seat_budget_is_shared_across_screens_and_survives_restart(self):
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1000)
  self.db.close();self.db=connect(self.path)
  with self.assertRaises(source.SourceWait) as error:source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1050)
  self.assertEqual(error.exception.until,1060)
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1060)
 def test_manual_reads_have_their_own_budget_but_honor_cooldowns(self):
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1000)
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1030,manual=True)
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1045,manual=True)
  for n in range(1,10):source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1030+n*30,manual=True)
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1330,manual=True)
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1330)
  self.db.execute("INSERT INTO source_hosts VALUES('services-in.bookmyshow.com',9999)");self.db.commit()
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',4700,manual=True)
 def test_seat_metadata_lane_is_not_starved_by_listing_checks(self):
  for n in range(5):source.reserve(self.db,'https://in.bookmyshow.com/api/x',1000+n*60)
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://in.bookmyshow.com/api/x',1250)
  source.reserve(self.db,'https://in.bookmyshow.com/api/x',1270,metadata=True)
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://in.bookmyshow.com/api/x',1299,metadata=True)
 def test_rolling_hour_cap_counts_every_attempt(self):
  for n in range(60):source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1000+n*60)
  with self.assertRaises(source.SourceWait) as error:source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',4599)
  self.assertEqual(error.exception.until,4600)
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',4600)
 def test_retry_after_persists_and_logs_only_safe_response_fields(self):
  key=source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx?private=ignored',1000)
  headers=Message();headers['Retry-After']='33792';headers['CF-Ray']='test-ray';headers['Set-Cookie']='private'
  with patch('http_source.time.time',return_value=1000):source.finish(self.db,key,429,headers)
  with self.assertRaises(source.SourceWait) as error:source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',2000)
  self.assertEqual(error.exception.until,34792)
  row=dict(self.db.execute('SELECT * FROM source_requests').fetchone())
  self.assertEqual(row['endpoint'],'/doTrans.aspx');self.assertEqual(row['cf_ray'],'test-ray');self.assertNotIn('private',str(row))
 def test_carried_over_deadline_blocks_seat_reads_and_only_extends_waits(self):
  from store import meta
  applied=source.carry_over(self.db,'services-in.bookmyshow.com=5000,in.bookmyshow.com=500',1000)
  self.assertEqual(applied,{'services-in.bookmyshow.com':5000.0})
  self.assertEqual(float(meta(self.db,'seat_cooldown_until')),5000)
  with self.assertRaises(source.SourceWait) as error:source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',4999)
  self.assertEqual(error.exception.until,5000)
  source.carry_over(self.db,'services-in.bookmyshow.com=3000',1000)
  self.assertEqual(float(meta(self.db,'seat_cooldown_until')),5000)
  for bad in ('example.com=5000','services-in.bookmyshow.com=nan','services-in.bookmyshow.com=inf'):
   with self.assertRaises(ValueError):source.carry_over(self.db,bad,1000)
 def test_listing_and_seat_hosts_have_independent_budgets(self):
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',1000)
  source.reserve(self.db,'https://in.bookmyshow.com/api/test',1000)
  with self.assertRaises(source.SourceWait):source.reserve(self.db,'https://in.bookmyshow.com/api/test',1029)
  source.reserve(self.db,'https://in.bookmyshow.com/api/test',1030)
 def test_budget_denial_sends_no_network_request(self):
  source.reserve(self.db,'https://services-in.bookmyshow.com/doTrans.aspx',time.time())
  with patch.dict(os.environ,{'BMS_REQUEST_BUDGET':'1','BMS_DB':self.path}),patch('urllib.request.urlopen') as call:
   with self.assertRaises(source.SourceWait):source.open_request(urllib.request.Request('https://services-in.bookmyshow.com/doTrans.aspx'))
  call.assert_not_called()
 def test_default_retains_standard_client(self):
  with patch.dict(os.environ,{'BMS_REQUEST_BUDGET':''}),patch('urllib.request.urlopen',return_value=io.BytesIO(b'ok')) as call:
   self.assertEqual(source.open_request(urllib.request.Request('https://in.bookmyshow.com/test')).read(),b'ok')
   call.assert_called_once()
