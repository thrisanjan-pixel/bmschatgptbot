import copy,json,os,tempfile,time,unittest
import threading,urllib.request,urllib.parse
from http.server import ThreadingHTTPServer
from dashboard import handler
from pathlib import Path
from unittest.mock import patch
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
 def test_specific_row_release_and_silent_baseline(self):
  self.configure();self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
  changed=copy.deepcopy(self.result);changed['available'].append({'row_id':'Gold / B','seat':'2'})
  row_watch.ingest(self.db,self.show,changed);self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
  changed['available'].append({'row_id':'Gold / A','seat':'3'})
  row_watch.ingest(self.db,self.show,changed)
  self.assertIn('Gold / A: 3',self.db.execute('select text from outbox').fetchone()[0])
  row_watch.ingest(self.db,self.show,changed);self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],1)
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
