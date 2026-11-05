import json,tempfile,time,unittest
from pathlib import Path
from store import connect,record_check,emit
import backup,watch_preferences as prefs
class BackupReportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_roundtrip_preserves_state_and_pauses_without_messages_or_sessions(self):
  from tests.test_rows import RowTests
  f=RowTests();f.setUp()
  try:
   f.configure();data=backup.export(f.db);raw=json.dumps(data).encode()
   self.assertNotIn('web_sessions',data['tables']);self.assertNotIn('meta',data['tables']);self.assertNotIn('outbox',data['tables'])
   r=backup.restore(self.db,raw);self.assertEqual(r['restored'],1);rule=prefs.rules(self.db)[0];self.assertFalse(rule['preferences']['enabled'])
   self.assertEqual(rule['preferences']['rows'],['Gold / A'])
   self.assertEqual(self.db.execute('SELECT count(*) FROM row_layouts').fetchone()[0],1)
   self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
   self.assertEqual(backup.restore(self.db,raw)['restored'],0)
  finally:f.tearDown()
 def test_invalid_schema_rejected_before_import(self):
  alert=prefs.save(self.db,{**prefs.defaults(),'movies':['Keep']});data=backup.export(self.db)
  data['tables']['sessions']=[{'id':'bad','payload':json.dumps({'url':'https://in.bookmyshow.com/test'}),'unexpected':True}]
  with self.assertRaises(ValueError):backup.restore(self.db,json.dumps(data).encode())
  self.assertEqual(prefs.rules(self.db)[0]['id'],alert)
 def test_same_volume_hourly_copy_roundtrip(self):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie']});backup.automatic(self.db,self.path);backup.automatic(self.db,self.path)
  files=list((Path(self.path).parent/'backups').glob('*.json'));self.assertEqual(len(files),1);self.assertEqual(backup.decode(files[0].read_bytes())['version'],1)
 def test_report_uses_measured_average_and_delivery_queue_delay(self):
  alert=prefs.save(self.db,{**prefs.defaults(),'movies':['Movie']})
  self.db.execute('INSERT INTO jobs(id,kind,payload) VALUES(?,?,?)',('job','movie',json.dumps({'movie':'Movie'})))
  now=time.time()+1;record_check(self.db,'listings','job',now,now=now);record_check(self.db,'listings','job',now+30,now=now+30);record_check(self.db,'listings','job',now+90,now=now+90)
  emit(self.db,'event','message',alert_id=alert);self.db.execute('UPDATE outbox SET delivered=created+4');self.db.commit()
  report=prefs.rules(self.db)[0]['report'];self.assertEqual(report['average_gap'],45);self.assertEqual(report['longest_gap'],60);self.assertEqual(report['delivery_average'],4)

 def test_old_backups_are_pruned_even_before_next_hourly_backup(self):
  from store import setmeta,meta
  folder=Path(self.path).parent/'backups';folder.mkdir()
  for i in range(10):(folder/('screenwatch-'+str(100+i)+'.json')).write_bytes(b'x'*10)
  (folder/'other.json').write_bytes(b'keep')
  setmeta(self.db,'last_backup',time.time());self.db.commit()
  backup.automatic(self.db,self.path)
  self.assertEqual(sorted(p.name for p in folder.glob('screenwatch-*.json')),['screenwatch-107.json','screenwatch-108.json','screenwatch-109.json'])
  self.assertTrue((folder/'other.json').exists())
  storage=json.loads(meta(self.db,'storage_usage'));self.assertEqual(storage['removed_backup_bytes'],70)

 def test_backup_byte_budget_preserves_newest_copy(self):
  folder=Path(self.path).parent/'backups';folder.mkdir()
  for i in range(4):(folder/('screenwatch-'+str(i)+'.json')).write_bytes(b'x'*10)
  result=backup.prune_backups(folder,count=3,budget=15)
  self.assertEqual(result['count'],1);self.assertEqual(result['bytes'],10)
  self.assertTrue((folder/'screenwatch-3.json').exists())

 def test_history_cleanup_preserves_pending_messages_and_alert_baselines(self):
  from store import setmeta
  now=time.time();alert=prefs.save(self.db,{**prefs.defaults(),'movies':['Movie']})
  for key in ['pending','old','recent']:
   emit(self.db,key,key,b'image',alert_id=alert)
  self.db.execute('UPDATE outbox SET created=?',(now-10*86400,))
  self.db.execute("UPDATE outbox SET delivered=? WHERE text='old'",(now-8*86400,))
  self.db.execute("UPDATE outbox SET delivered=? WHERE text='recent'",(now-2*86400,))
  self.db.execute('CREATE TABLE IF NOT EXISTS row_alert_state(alert_id TEXT PRIMARY KEY,signature TEXT,available TEXT)')
  self.db.execute('INSERT INTO row_alert_state VALUES(?,?,?)',(alert,'baseline','[]'));self.db.commit()
  backup.cleanup_history(self.db,now)
  items={r['text']:r['photo'] for r in self.db.execute('SELECT text,photo FROM outbox')}
  self.assertEqual(items,{'pending':b'image','recent':None})
  self.assertEqual(self.db.execute('SELECT signature FROM row_alert_state').fetchone()[0],'baseline')
  self.assertEqual(self.db.execute('SELECT count(*) FROM alerts').fetchone()[0],1)
