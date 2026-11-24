"""Any-showtime row alerts, urgent alerts, presets, alert history and the installable app files."""
import copy,hashlib,json,os,tempfile,threading,time,unittest,urllib.error,urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from dashboard import handler
from store import connect,emit,setmeta,meta
import api_watch,row_watch,health_watch
import watch_preferences as prefs

NOW=time.time()
def show(i,movie='Movie',venue='TEST',minutes=60,date='20261231',language='Telugu • 2D'):
 return {'id':f's{i}','event':'ET1','movie':movie,'venue':venue+' Cinema','venue_code':venue,'date':date,'time':'07:00 PM','language':language,'format':'GOLD',
  'categories':{'1':{'name':'Gold','price':'200','status':'1'}},'cutoff':NOW+minutes*60,'url':'https://in.bookmyshow.com/x'}
def any_prefs(**extra):
 return {**prefs.defaults(),'movies':['Movie'],'cinemas':['TEST'],'row_show':'*','rows':['Gold / B'],'enabled':True,'categories':['Gold'],**extra}
def result(*seats):
 return {'rows':[{'id':'Gold / A','category':'Gold','row':'A'},{'id':'Gold / B','category':'Gold','row':'B'}],
  'available':[{'row_id':'Gold / B','seat':s} for s in seats],'seats':[{'row_id':'Gold / B','seat':s,'available':True} for s in seats],'url':'https://in.bookmyshow.com/seat-layout/x'}

class Base(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'s.db');self.db=connect(self.path)
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def add(self,*shows,active=1):
  for s in shows:self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)',(s['id'],json.dumps(s),s['cutoff'],active))
  self.db.commit()
 def alert(self,**extra):
  prefs.save(self.db,any_prefs(**extra),create=True)
  return prefs.rules(self.db)[0]
 def client(self,name='extension',age=0):
  setmeta(self.db,f'pc_{name}_seen',time.time()-age);self.db.commit()

class AnyShowTests(Base):
 def test_validate(self):
  self.assertEqual(prefs.validate(any_prefs())['row_show'],'*')
  for bad,text in [({'rows':[]},'row'),({'cinemas':[]},'cinema'),({'cinemas':['A','B']},'cinema'),({'movies':[],'enabled':False},'movie')]:
   with self.assertRaises(ValueError) as caught:prefs.validate(any_prefs(**bad))
   self.assertIn(text,str(caught.exception))
  with self.assertRaises(ValueError):prefs.validate({**prefs.defaults(),'urgent':'yes'})
  self.assertFalse(prefs.defaults()['urgent']);self.assertTrue(prefs.validate({**prefs.defaults(),'urgent':True})['urgent'])
 def test_targets_filter_sort_and_cap(self):
  self.add(*[show(i,minutes=100-i) for i in range(25)],show('x1',movie='Other'),show('x2',venue='ELSE'),show('x3',minutes=-5),show('x4',language='Hindi • 2D'),show('x5',minutes=5))
  self.db.execute("UPDATE sessions SET active=0 WHERE id='s24'");self.db.commit()
  p=prefs.validate(any_prefs(languages=['Telugu']))
  ids=prefs.row_targets(self.db,p)
  self.assertEqual(len(ids),prefs.ROW_ANY_LIMIT);self.assertEqual(ids[0],'sx5');self.assertEqual(ids[1],'s23')
  self.assertNotIn('sx1',ids);self.assertNotIn('sx2',ids);self.assertNotIn('sx3',ids);self.assertNotIn('sx4',ids);self.assertNotIn('s24',ids)
  self.assertEqual(prefs.row_targets(self.db,{**prefs.defaults()}),[])
  self.assertEqual(prefs.row_targets(self.db,{**prefs.defaults(),'row_show':'abc'}),['abc'])
 def test_rules_report_targets_and_never_expire_by_showtime(self):
  self.add(show(1),show(2));rule=self.alert()
  self.assertEqual(rule['row_targets'],2);self.assertEqual(rule['status'],'active')
  self.assertEqual(prefs.rules(self.db)[0]['row_targets'],2)
  plain=prefs.save(self.db,{**prefs.defaults(),'movies':['Movie']},create=True)
  self.assertEqual(next(r for r in prefs.rules(self.db) if r['id']==plain)['row_targets'],0)
 def test_health_uses_oldest_read_latest_error_and_total_targets(self):
  self.add(show(1),show(2),show(3));self.alert();row_watch.schema(self.db);now=time.time()
  for sid,checked,error in (('s1',now-500,''),('s2',now-200,'Seat source HTTP 403'),('s3',0,'')):
   self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,checked,error,due) VALUES(?,'{}',?,?,0)",(sid,checked,error))
  from store import record_check
  record_check(self.db,'rows','s1',now-100,'',now-100);record_check(self.db,'rows','s2',now-50,'Seat source HTTP 403',now-50)
  self.db.commit();h=prefs.rules(self.db)[0]['health']
  self.assertAlmostEqual(h['last_success'],now-500,delta=1);self.assertEqual(h['error'],'Seat source HTTP 403')
  # 3 showtimes: quiet period 720 s, so 500 s old is not "delayed" (one showtime would allow only 240 s).
  self.db.execute("UPDATE row_layouts SET error=''");self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['health']['label'],'Watching seats')
  self.db.execute("DELETE FROM sessions WHERE id IN ('s2','s3')");self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['health']['label'],'Checks delayed')
  self.db.execute("DELETE FROM row_layouts");self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['health']['last_success'],0)
 def test_health_watch_counts_targets(self):
  now=100000.0;setmeta(self.db,'health_watch_started',1);setmeta(self.db,'city_heartbeat',now);setmeta(self.db,'row_reader_heartbeat',now)
  rule={'preferences':{**prefs.defaults(),'row_show':'*','movies':['M']},'health':{'last_success':now-1000,'error':''},'created':0,'row_targets':5}
  with patch.object(prefs,'active',return_value=[rule]):
   self.assertEqual(health_watch.problems(self.db,now),{})
   rule['row_targets']=1;self.assertIn('source:*',health_watch.problems(self.db,now))
   rule['row_targets']=0;self.assertEqual(health_watch.problems(self.db,now),{})
 def test_schedule_queues_every_target_and_creates_rows(self):
  self.add(show(1),show(2),show(3,movie='Other'));self.alert();now=time.time()
  row_watch.schedule_active(self.db,now)
  rows={r['show_id']:r['due'] for r in self.db.execute('SELECT show_id,due FROM row_layouts')}
  self.assertEqual(set(rows),{'s1','s2'});self.assertTrue(all(d==now for d in rows.values()))
  self.assertEqual(row_watch.queue_order(self.db,now)[2],{'s1','s2'})
  self.assertEqual({q['show_id'] for q in row_watch.queue(self.db,now)},{'s1','s2'})
 def test_queue_rotates_fairly_over_targets(self):
  self.add(*[show(i) for i in range(5)]);self.alert();now=time.time();picked=[]
  for _ in range(15):
   row_watch.schedule_active(self.db,now,interval=60);job=row_watch.next_job(self.db,now)
   if job:
    picked.append(job['show_id']);self.db.execute("UPDATE row_layouts SET due=0,checked=? WHERE show_id=?",(now,job['show_id']));self.db.commit()
   now+=60
  self.assertEqual(sorted(picked[:5]),['s0','s1','s2','s3','s4'])
  self.assertEqual(picked[:5],picked[5:10]);self.assertEqual(picked[:5],picked[10:15])
 def test_seat_memory_is_kept_per_showtime(self):
  a,b=show('A'),show('B');self.add(a,b);rule=self.alert();rid=rule['id']
  def reset():self.db.execute('UPDATE alerts SET last_alert=0');self.db.execute("UPDATE outbox SET delivered=1");self.db.commit()
  row_watch.ingest(self.db,a,result('1'));self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
  reset();row_watch.ingest(self.db,b,result('1'))
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)  # seats open at A did not hide B's
  texts=[r[0] for r in self.db.execute('SELECT text FROM outbox ORDER BY created')]
  self.assertTrue(all('07:00 PM' in t and '31 Dec 2026' in t for t in texts))
  self.assertEqual({r[0] for r in self.db.execute('SELECT alert_id FROM row_alert_state')},{rid+':sA',rid+':sB'})
  reset();row_watch.ingest(self.db,a,result('1'));self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)  # unchanged: silent
  reset();row_watch.ingest(self.db,a,result('1','2'));self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],3)
  other=show('C',movie='Other');self.add(other);reset();row_watch.ingest(self.db,other,result('1'))
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],3)  # not a target
  prefs.delete(self.db,rid);self.assertEqual(self.db.execute('SELECT count(*) FROM row_alert_state').fetchone()[0],0)
 def test_listing_checks_follow_any_show_alerts(self):
  self.alert(movies=['Epic'],dates=['20261231'])
  self.assertEqual(api_watch.watched_movies(self.db),['epic'])
  wanted=api_watch.wanted_dates(self.db)
  self.assertIn('epic',wanted['movies']);self.assertEqual(wanted['movies']['epic'][0][0],{'20261231'})
  self.assertTrue(api_watch.is_watched({'movie':'Epic','event':'E','date':'20261231'},wanted))

def simulate(order,jobs,turns=30):
 """Advance the clock a minute a turn; the picked job is polled again in a minute."""
 now=1_000_000.0;due={j:now-60 for j in jobs};picks=[];last={j:0 for j in jobs};longest=0
 for turn in range(turns):
  now+=60;choice=order(due,now);picks.append(choice)
  for j in jobs:longest=max(longest,turn-last[j]) if j!=choice else longest
  last[choice]=turn;due[choice]=now+60
 return picks,longest

class UrgentTests(Base):
 def test_urgent_listing_gets_about_half_the_turns(self):
  for name in ('u','a','b'):
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,0)',(name,'discovery','https://x/'+name,json.dumps({'movie':name,'event':'E'+name,'date':''})))
  self.db.commit()
  self.alert(row_show='',rows=[],movies=['u'],urgent=True,categories=[]);prefs.save(self.db,{**prefs.defaults(),'movies':['a'],'enabled':True},create=True);prefs.save(self.db,{**prefs.defaults(),'movies':['b'],'enabled':True},create=True)
  state={'due':None}
  def order(due,now):
   for j,d in due.items():self.db.execute('UPDATE jobs SET due=?,checked=0 WHERE id=?',(d,j))
   self.db.commit();setmeta(self.db,'listing_background_attempt',now)
   return api_watch.listing_order(self.db,now)[0]['id']
  picks,longest=simulate(order,['u','a','b'])
  share=picks.count('u')/len(picks)
  self.assertTrue(0.4<=share<=0.6,share);self.assertLessEqual(longest,6)
  self.assertTrue(all(picks.count(j)>=3 for j in ('a','b')))
 def test_without_urgent_listings_share_evenly(self):
  for name in ('u','a','b'):
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,0)',(name,'discovery','https://x/'+name,json.dumps({'movie':name,'event':'E'+name,'date':''})))
   self.db.commit();prefs.save(self.db,{**prefs.defaults(),'movies':[name],'enabled':True},create=True)
  def order(due,now):
   for j,d in due.items():self.db.execute('UPDATE jobs SET due=?,checked=0 WHERE id=?',(d,j))
   self.db.commit();return api_watch.listing_order(self.db,now)[0]['id']
  picks,_=simulate(order,['u','a','b']);self.assertEqual(picks.count('u'),10)
 def test_urgent_never_checked_listing_keeps_its_place(self):
  now=1000.0
  for name,due in (('u',0),('a',now-400)):
   self.db.execute('INSERT INTO jobs(id,kind,url,payload,due) VALUES(?,?,?,?,?)',(name,'discovery','https://x/'+name,json.dumps({'movie':name,'event':'E'+name,'date':''}),due))
  self.db.commit();self.alert(row_show='',rows=[],movies=['u'],urgent=True,categories=[]);prefs.save(self.db,{**prefs.defaults(),'movies':['a'],'enabled':True},create=True)
  self.assertEqual([r['id'] for r in api_watch.listing_order(self.db,now)],['a','u'])
 def test_urgent_seat_reads_get_about_half_the_turns(self):
  self.add(*[show(i) for i in range(3)])
  prefs.save(self.db,any_prefs(movies=['Movie'],urgent=True,cinemas=['U'],rows=['Gold / B']),create=True)
  self.add(show('u',venue='U'),show('n1',venue='N1'),show('n2',venue='N2'))
  self.db.execute("DELETE FROM sessions WHERE id IN ('s0','s1','s2')");self.db.commit()
  for v in ('N1','N2'):prefs.save(self.db,any_prefs(cinemas=[v]),create=True)
  self.db.commit();row_watch.schema(self.db);jobs=['su','sn1','sn2']
  self.assertEqual(row_watch.targets(self.db)[1],{'su'});self.assertEqual(set(row_watch.targets(self.db)[0]),set(jobs))
  def order(due,now):
   for j in jobs:self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,checked,due) VALUES(?,'{}',?,?)",(j,now-500,due[j]))
   self.db.commit();return row_watch.next_job(self.db,now)['show_id']
  picks,longest=simulate(order,jobs)
  share=picks.count('su')/len(picks)
  self.assertTrue(0.4<=share<=0.6,share);self.assertLessEqual(longest,6)
 def test_urgent_showtime_counts_as_overdue_sooner(self):
  self.add(show('u',venue='U'),show('n',venue='N'));row_watch.schema(self.db)
  prefs.save(self.db,any_prefs(cinemas=['U'],urgent=True),create=True);prefs.save(self.db,any_prefs(cinemas=['N']),create=True)
  now=time.time()
  for sid,age in (('su',50),('sn',100)):self.db.execute("INSERT INTO row_layouts(show_id,payload,checked,due) VALUES(?,'{}',?,?)",(sid,now-900,now-age))
  self.db.commit()
  # Both test shows start within 3 hours; turn the show-day boost off to test the urgent flag alone.
  with patch.dict(os.environ,{'BMS_SHOWDAY_WINDOW':'0'}):
   self.assertEqual(row_watch.next_job(self.db,now)['show_id'],'su')  # 50 s overdue counts as 150 s; the other is below 2 minutes

class PresetTests(Base):
 def test_save_replace_limit_delete(self):
  saved=prefs.save_preset(self.db,'  Late shows ',{**prefs.defaults(),'movies':['Movie'],'dates':['20261231'],'enabled':True,'time_from':'18:00','languages':['Telugu'],'row_show':'abc','rows':['x'],'party_size':3})
  p=saved[0]['preferences'];self.assertEqual(saved[0]['name'],'Late shows');self.assertEqual(len(saved[0]['id']),32)
  self.assertEqual((p['movies'],p['dates'],p['enabled'],p['request_state'],p['row_show'],p['rows'],p['party_size']),([],[],False,'','',[],0))
  self.assertEqual((p['time_from'],p['languages']),('18:00',['Telugu']))
  again=prefs.save_preset(self.db,'LATE SHOWS',{**prefs.defaults(),'time_from':'20:00'})
  self.assertEqual(len(again),1);self.assertEqual(again[0]['preferences']['time_from'],'20:00');self.assertEqual(again[0]['name'],'LATE SHOWS')
  any_show=prefs.save_preset(self.db,'Any',any_prefs(movies=[]))[-1]['preferences']
  self.assertEqual((any_show['row_show'],any_show['rows'],any_show['enabled'],any_show['movies']),('*',['Gold / B'],False,[]))
  with self.assertRaises(ValueError):prefs.save_preset(self.db,'Bad',any_prefs(cinemas=[]))
  for bad in ('','   ','x'*41,5):
   with self.assertRaises(ValueError):prefs.save_preset(self.db,bad,prefs.defaults())
  for i in range(28):prefs.save_preset(self.db,f'p{i}',prefs.defaults())
  with self.assertRaises(ValueError):prefs.save_preset(self.db,'one more',prefs.defaults())
  self.assertEqual(len(prefs.save_preset(self.db,'p3',prefs.defaults())),30)
  kept=prefs.delete_preset(self.db,prefs.presets(self.db)[0]['id']);self.assertEqual(len(kept),29)
  with self.assertRaises(ValueError):prefs.delete_preset(self.db,'nope')

class HistoryTests(Base):
 def test_history_summaries(self):
  self.add(show(1));row=self.alert();plain=prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'enabled':True},create=True)
  link='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/TEST/1/1/20261231'
  emit(self.db,'r1','🎟 3 seats now available · Rows Gold / B\n\n🎬 Movie\n\n'+link,alert_id=row['id'])
  emit(self.db,'p1','🎬 Movie\nTelugu • 2D\n📅 31 Dec 2026\n📍 Test Cinema\n\nCategory now available · 07:00 PM · 3D GOLD (₹395)\n'+link+'\n',alert_id=plain)
  emit(self.db,'h1','⚠️ Screenwatch needs attention',alert_id='__health__')
  for i in range(12):emit(self.db,f'm{i}','Note '+str(i),alert_id=plain)
  self.db.execute("UPDATE outbox SET delivered=5 WHERE text LIKE '%now available · Rows%'")
  self.db.execute("UPDATE outbox SET created=100 WHERE text LIKE 'Category%' OR text LIKE '%Category now%'");self.db.commit()
  h=prefs.history(self.db)
  self.assertNotIn('__health__',h);self.assertEqual(len(h[plain]),10)
  r=h[row['id']][0];self.assertEqual(r['summary'],'🎟 3 seats now available · Rows Gold / B');self.assertEqual(r['url'],link);self.assertEqual(r['delivered'],5)
  self.assertTrue(all(x['delivered']==0 for x in h[plain]))
  p=prefs.history(self.db,limit=50)[plain][-1]
  self.assertEqual(p['summary'],'Category now available · 07:00 PM · 3D GOLD (₹395)');self.assertEqual(p['url'],link)
  self.assertLessEqual(len(prefs.history_summary('x'*400,True)),140)

class ServerTests(Base):
 def serve(self,public=False):
  server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.path,public=public))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
  return f'http://127.0.0.1:{server.server_port}'
 def call(self,url,body=None,csrf=''):
  request=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers={'X-CSRF-Token':csrf})
  try:
   with urllib.request.urlopen(request) as r:return r.status,json.loads(r.read())
  except urllib.error.HTTPError as error:
   try:
    text=error.read()
    try:return error.code,json.loads(text)
    except ValueError:return error.code,{}
   finally:error.close()
 def test_api_presets_history_and_limits(self):
  self.add(show(1));row=self.alert();url=self.serve()
  emit(self.db,'r1','🎟 2 seats now available · Row Gold / B\n\nhttps://in.bookmyshow.com/a',alert_id=row['id']);self.db.commit()
  code,data=self.call(url+'/api/watch');csrf=data['csrf']
  self.assertEqual((data['presets'],data['row_any_limit']),([],20));self.assertEqual(data['alerts'][0]['row_targets'],1)
  self.assertEqual(data['alerts'][0]['history'][0]['summary'],'🎟 2 seats now available · Row Gold / B')
  self.assertEqual(self.call(url+'/api/watch',{'action':'save_preset','name':'A','preferences':prefs.defaults()})[0],403)
  code,data=self.call(url+'/api/watch',{'action':'save_preset','name':'A','preferences':{**prefs.defaults(),'urgent':True}},csrf)
  self.assertEqual(code,200);pid=data['presets'][0]['id'];self.assertTrue(data['presets'][0]['preferences']['urgent'])
  self.assertEqual(self.call(url+'/api/watch')[1]['presets'][0]['id'],pid)
  code,data=self.call(url+'/api/watch',{'action':'save_preset','name':'','preferences':{}},csrf);self.assertEqual(code,400);self.assertIn('error',data)
  self.assertEqual(self.call(url+'/api/watch',{'action':'save_preset','name':'B'},csrf)[0],400)
  self.assertEqual(self.call(url+'/api/watch',{'action':'delete_preset','id':'nope'},csrf)[0],400)
  code,data=self.call(url+'/api/watch',{'action':'delete_preset','id':pid},csrf);self.assertEqual((code,data),(200,{'presets':[]}))
  with patch.dict(os.environ,{'BMS_ROWS_ENABLED':'1'}):self.assertEqual(self.call(url+'/api/watch',{**any_prefs(),'rows':['Gold / B']},csrf)[0],200)  # a '*' alert saves without a layout
  self.assertEqual(self.call(url+'/api/watch',{**any_prefs(cinemas=[]),'create':True},csrf)[0],400)

 def test_manifest_and_icons_are_public_and_whitelisted(self):
  with patch.dict(os.environ,{'DASHBOARD_PASSWORD':'test-password-at-least-16'}):url=self.serve(public=True)
  for path,kind in (('/manifest.json','application/manifest+json'),('/static/icon-180.png','image/png'),('/static/icon-192.png','image/png'),('/static/icon-512.png','image/png')):
   with urllib.request.urlopen(url+path) as r:
    self.assertEqual(r.headers['Content-Type'],kind);self.assertIn('max-age',r.headers['Cache-Control']);body=r.read()
   if kind=='image/png':self.assertEqual(body[:8],b'\x89PNG\r\n\x1a\n')
  manifest=json.loads(urllib.request.urlopen(url+'/manifest.json').read())
  self.assertEqual((manifest['name'],manifest['display'],manifest['start_url'],manifest['theme_color']),('Screenwatch','standalone','/','#0b0c10'))
  self.assertEqual({i['sizes'] for i in manifest['icons']},{'192x192','512x512'})
  for path in ('/static/../dashboard.py','/static/screenwatch.html','/static/icon-1.png','/static/%2e%2e/dashboard.py','/manifest.json/x'):
   with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+path)
   self.assertIn(caught.exception.code,(401,404));caught.exception.close()
  dockerfile=Path(__file__).resolve().parents[1].joinpath('Dockerfile').read_text()
  for name in ('manifest.json','icon-180.png','icon-192.png','icon-512.png'):self.assertIn('static/'+name,dockerfile)

class OpenOnPcTests(Base):
 call=ServerTests.call
 def test_preference_default_validation_and_presets(self):
  self.assertFalse(prefs.defaults()['open_on_pc']);self.assertTrue(prefs.validate({**prefs.defaults(),'open_on_pc':True})['open_on_pc'])
  with self.assertRaises(ValueError):prefs.validate({**prefs.defaults(),'open_on_pc':'yes'})
  self.assertFalse(prefs.preset_preferences({**prefs.defaults(),'open_on_pc':True})['open_on_pc'])
 def test_pc_alerts_filtering(self):
  self.client();self.add(show(1));on=self.alert(open_on_pc=True)['id'];off=prefs.save(self.db,any_prefs(open_on_pc=False),create=True)
  seat='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET1/TEST/1/1/20261231'
  now=time.time()
  def put(key,text,alert,age):
   emit(self.db,key,text,alert_id=alert);self.db.execute('UPDATE outbox SET created=? WHERE id=?',(now-age,hashlib.sha256(key.encode()).hexdigest()))
  ids=lambda *keys:[hashlib.sha256(k.encode()).hexdigest() for k in keys]
  put('old','🎟 old\n\n'+seat,on,700);put('a','🎟 first\n\nhttps://in.bookmyshow.com/movies/x\n'+seat+'\nhttps://in.bookmyshow.com/z',on,30)
  put('b','🎟 second\n\n'+seat,on,20);put('off','🎟 off\n\n'+seat,off,20);put('hl','⚠️ health\n'+seat,'__health__',20)
  put('nolink','🎟 nolink\n\nhttps://evil.example/x',on,10);put('plain','🎟 plain, no link',on,9)
  for i in range(6):put(f'm{i}','🎟 m'+str(i)+'\n'+seat,on,8-i)
  self.db.commit()
  r=prefs.pc_alerts(self.db,0,now)
  self.assertTrue(r['armed']);self.assertEqual(r['now'],now);self.assertEqual([e['id'] for e in r['events']],ids('a','b','m0','m1','m2'))
  first=r['events'][0];self.assertEqual((first['alert'],first['url'],first['summary']),(on,seat,'🎟 first'));self.assertAlmostEqual(first['created'],now-30,3)
  self.assertEqual([e['id'] for e in prefs.pc_alerts(self.db,now-25,now)['events']],ids('b','m0','m1','m2','m3'))
  self.assertEqual(prefs.pc_alerts(self.db,now,now)['events'],[])
  prefs.set_open_on_pc(self.db,on,False);r=prefs.pc_alerts(self.db,0,now);self.assertEqual((r['armed'],r['events']),(False,[]))
  prefs.save(self.db,any_prefs(open_on_pc=True,enabled=False),alert_id=on)
  self.assertFalse(prefs.pc_alerts(self.db,0,now)['armed'])
 def test_api_pc_alerts_and_pc_opened(self):
  self.client();self.add(show(1));row=self.alert(open_on_pc=True,urgent=True)
  server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.path));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()));url=f'http://127.0.0.1:{server.server_port}'
  seat='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00523251/ALUC/6249/20261111'
  emit(self.db,'r1','🎟 2 seats\n\n⭐ Best 2 together: Gold / B · seats 09, 08\n\n'+seat,alert_id=row['id']);self.db.commit()
  code,data=self.call(url+'/api/pc-alerts?since=0');self.assertEqual(code,200);self.assertTrue(data['armed']);self.assertEqual([e['id'] for e in data['events']],[hashlib.sha256(b'r1').hexdigest()])
  self.assertEqual(data['events'][0]['pair'],{'category':'Gold','row':'B','seats':['09','08']})
  self.assertEqual(self.call(url+'/api/pc-alerts?since=abc')[0],200)
  csrf=self.call(url+'/api/watch')[1]['csrf']
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_opened','alert':row['id']})[0],403)
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_opened','alert':'nope'},csrf)[0],400)
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_opened','alert':row['id'],'enabled':False},csrf)[0],400)
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_opened','alert':row['id']},csrf),(200,{'ok':True}))
  p=prefs.rules(self.db)[0]['preferences'];self.assertFalse(p['open_on_pc']);self.assertTrue(p['urgent'] and p['enabled']);self.assertEqual(p['rows'],['Gold / B'])
  self.assertFalse(self.call(url+'/api/pc-alerts?since=0')[1]['armed'])
  # pc_cancel turns the arm off the same way, and the page and pc-alerts replies carry the new fields
  prefs.set_open_on_pc(self.db,row['id'],True);self.assertEqual(self.call(url+'/api/watch',{'action':'pc_cancel','alert':row['id']})[0],403)
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_cancel','alert':'nope'},csrf)[0],400)
  self.assertEqual(self.call(url+'/api/watch',{'action':'pc_cancel','alert':row['id']},csrf),(200,{'ok':True}))
  watch=self.call(url+'/api/watch')[1];self.assertEqual(watch['alerts'][0]['preferences']['open_on_pc_since'],0);self.assertEqual(watch['alerts'][0]['open_on_pc_since'],0)
  self.assertEqual(set(watch['pc_clients']),{'extension'});self.assertLess(watch['pc_clients']['extension'],60)
  data=self.call(url+'/api/pc-alerts?since=0&client=helper')[1];self.assertTrue(data['extension']);self.assertEqual(data['armed_alerts'],[])
  self.assertEqual(meta(self.db,'pc_helper_seen'),'')  # only the extension counts as a PC client

class PcExtensionServerTests(Base):
 def test_arm_has_no_time_limit(self):
  self.client();self.add(show(1));row=self.alert(open_on_pc=True);t=time.time()
  since=row['preferences']['open_on_pc_since'];self.assertAlmostEqual(since,t,delta=5);self.assertNotIn('open_on_pc_until',row['preferences'])
  # an ordinary edit keeps the arm time; turning off clears it and works with no client
  self.db.execute("UPDATE meta SET value='0' WHERE key LIKE 'pc_%_seen'");self.db.commit()
  prefs.save(self.db,{**row['preferences'],'urgent':True},alert_id=row['id']);self.assertEqual(prefs.rules(self.db)[0]['preferences']['open_on_pc_since'],since)
  p=row['preferences'];prefs.save(self.db,{**p,'open_on_pc':False},alert_id=row['id']);self.assertEqual(prefs.rules(self.db)[0]['preferences']['open_on_pc_since'],0)
  # an arm stays on long after the old 3-hour limit
  self.client();prefs.set_open_on_pc(self.db,row['id'],True)
  stored=lambda:json.loads(self.db.execute('SELECT payload FROM alerts WHERE id=?',(row['id'],)).fetchone()[0])
  p=stored();p['open_on_pc_since']=time.time()-30*3600;self.db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),row['id']));self.db.commit()
  r=prefs.pc_alerts(self.db,0);self.assertEqual([a['id'] for a in r['armed_alerts']],[row['id']]);self.assertTrue(stored()['open_on_pc'])
  # arms saved by older builds: a live expiry becomes an arm time, an expired or expiry-less one is off
  p=stored();p.pop('open_on_pc_since');p.update(open_on_pc=True,open_on_pc_until=time.time()+3600);self.db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),row['id']));self.db.commit()
  p=prefs.rules(self.db)[0]['preferences'];self.assertTrue(p['open_on_pc']);self.assertAlmostEqual(stored()['open_on_pc_since'],time.time()-2*3600,delta=5);self.assertNotIn('open_on_pc_until',stored())
  p=stored();p.pop('open_on_pc_since');p.update(open_on_pc=True,open_on_pc_until=time.time()-1);self.db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),row['id']));self.db.commit()
  self.assertFalse(prefs.rules(self.db)[0]['preferences']['open_on_pc'])
  p=stored();p.update(open_on_pc=True,open_on_pc_until=0,open_on_pc_since=0);self.db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),row['id']));self.db.commit()
  self.assertFalse(prefs.rules(self.db)[0]['preferences']['open_on_pc'])  # a legacy arm with no start time is off
  self.assertEqual(prefs.preset_preferences({**prefs.defaults(),'open_on_pc':True,'open_on_pc_since':99})['open_on_pc_since'],0)
 def test_arming_needs_a_live_pc_client(self):
  self.add(show(1));message='Enable the Screenwatch extension in Chrome on this PC first'
  with self.assertRaises(ValueError) as caught:self.alert(open_on_pc=True)
  self.assertEqual(str(caught.exception),message)
  off=self.alert();self.assertEqual(prefs.pc_clients(self.db),{'extension':None})
  with self.assertRaises(ValueError):prefs.set_open_on_pc(self.db,off['id'],True)
  self.client('extension',age=200)
  with self.assertRaises(ValueError):prefs.set_open_on_pc(self.db,off['id'],True)  # seen more than 120 s ago
  self.client('helper',age=100)
  with self.assertRaises(ValueError):prefs.set_open_on_pc(self.db,off['id'],True)  # the old helper no longer counts
  self.client('extension',age=100);prefs.set_open_on_pc(self.db,off['id'],True);self.assertTrue(prefs.rules(self.db)[0]['preferences']['open_on_pc'])
  prefs.set_open_on_pc(self.db,off['id'],False);self.assertFalse(prefs.rules(self.db)[0]['preferences']['open_on_pc'])
  self.assertTrue(prefs.pc_alerts(self.db,0,client='extension')['extension']);self.assertFalse(prefs.pc_alerts(self.db,0,now=time.time()+500)['extension'])
  prefs.pc_alerts(self.db,0,client='other');self.assertEqual(meta(self.db,'pc_other_seen'),'')
 def test_row_alert_ignores_listing_messages(self):
  self.client();self.add(show(1));row=self.alert(open_on_pc=True);p=json.loads(self.db.execute('SELECT payload FROM alerts WHERE id=?',(row['id'],)).fetchone()[0])
  p.update(row_show='x',rows=['Gold / B']);self.db.execute('UPDATE alerts SET payload=? WHERE id=?',(json.dumps(p),row['id']))
  link='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00523251/ALUC/6249/20261111'
  emit(self.db,'old','🎬 Film'+chr(10)+'Seats already available for this alert:'+chr(10)+link,alert_id=row['id']);emit(self.db,'new','🎟 2 seats now available'+chr(10)+link,alert_id=row['id']);self.db.commit()
  events=prefs.pc_alerts(self.db,0)['events'];self.assertEqual(len(events),1);self.assertTrue(events[0]['summary'].startswith('🎟'))
 def test_event_show_and_pair(self):
  self.client();s={**show('x'),'id':'ALUC:20261111:6249:ET00523251','movie':'Big Film','venue':'Alu Cinema','date':'20261111','time':'09:40 PM','format':'PCX'};self.add(s)
  row=self.alert(open_on_pc=True);seat='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00523251/ALUC/6249/20261111'
  other='https://in.bookmyshow.com/movies/hyderabad/seat-layout/ET00523251/ALUC/9999/20261111'
  emit(self.db,'a','🎟 2 seats now available'+chr(10)*2+'⭐ Best 2 together: Gold / B · seats 09, 10\n\n'.replace('\\n','\n')+seat,alert_id=row['id']);emit(self.db,'b','🎟 plain\n'.replace('\\n','\n')+other,alert_id=row['id']);self.db.commit()
  first,second=prefs.pc_alerts(self.db,0)['events']
  self.assertEqual(first['show'],{'movie':'Big Film','venue':'Alu Cinema','date':'20261111','time':'09:40 PM','screen':'PCX'})
  self.assertEqual(first['pair'],{'category':'Gold','row':'B','seats':['09','10']});self.assertNotIn('show',second);self.assertNotIn('pair',second)

class SeatCountTests(Base):
 """Minimum seats open (row and category alerts) and the seat-map photo that follows every category alert."""
 def setUp(self):
  super().setUp();patcher=patch.dict(os.environ,{'BMS_ROWS_ENABLED':'1'});patcher.start();self.addCleanup(patcher.stop)
  from tests import test_api_watch as T
  self.T=T;self.job={'id':'discovery:'+T.show_url(T.EVENT,T.DAY),'payload':json.dumps({'event':T.EVENT,'date':T.DAY,'movie':'Test Movie','notify_first':False})}
 def cat_alert(self,**extra):
  prefs.save(self.db,{**prefs.defaults(),'movies':['Test Movie'],'enabled':True,'once':False,**extra},create=True)
  return prefs.rules(self.db)[0]['id']
 def open_show(self,*status):
  """Baseline listing with a sold-out Gold, then the given statuses in turn; returns the stored show."""
  api_watch.ingest(self.db,self.job,self.T.response(status='0'))
  for s in status or ('3',):api_watch.ingest(self.db,self.job,self.T.response(status=s))
  self.db.commit();return json.loads(self.db.execute('SELECT payload FROM sessions').fetchone()[0])
 def texts(self):return [r[0] for r in self.db.execute('SELECT text FROM outbox ORDER BY created,rowid')]
 def pending(self):return [dict(r) for r in self.db.execute('SELECT * FROM pending_seat_photos')]
 def read(self,show,res):
  """One seat read of the show: the queue picks it, then the result is ingested."""
  row_watch.schedule_active(self.db,time.time());job=row_watch.next_job(self.db,time.time());self.assertEqual(job['show_id'],show['id'])
  row_watch.ingest(self.db,show,res);self.db.commit()
 def test_validate_min_seats(self):
  self.assertEqual(prefs.defaults()['min_seats'],0);self.assertEqual(prefs.validate({**prefs.defaults()})['min_seats'],0)
  for good in (0,1,12,50):self.assertEqual(prefs.validate({**prefs.defaults(),'min_seats':good})['min_seats'],good)
  for bad in (51,-1,True,'3',2.5,None):
   with self.assertRaises(ValueError):prefs.validate({**prefs.defaults(),'min_seats':bad})
  self.assertEqual(prefs.preset_preferences({**prefs.defaults(),'min_seats':4})['min_seats'],4)
 def test_row_alert_waits_for_minimum(self):
  a=show('A');self.add(a);rid=self.alert(min_seats=3)['id']
  row_watch.ingest(self.db,a,result('1','2'));self.assertEqual(self.texts(),[])
  row_watch.ingest(self.db,a,result('1','2','3'))  # the third seat reaches the minimum: all three are named
  self.assertEqual(len(self.texts()),1);self.assertIn('3 seats now available',self.texts()[0])
  self.db.execute('UPDATE alerts SET last_alert=0');self.db.execute('UPDATE outbox SET delivered=1');self.db.commit()
  row_watch.ingest(self.db,a,result('1','2','3','4'));self.assertEqual(len(self.texts()),2);self.assertIn('1 seat now available',self.texts()[1])
  self.db.execute('UPDATE alerts SET last_alert=0');self.db.commit()
  row_watch.ingest(self.db,a,result('1','2','3','4'));self.assertEqual(len(self.texts()),2)  # nothing new
  self.assertEqual(prefs.rules(self.db)[0]['id'],rid)
 def test_row_alert_minimum_with_party(self):
  a=show('A');self.add(a);self.alert(min_seats=4,party_size=2)
  row_watch.ingest(self.db,a,result('1','2','3'));self.assertEqual(self.texts(),[])
  row_watch.ingest(self.db,a,result('1','2','3','9'));self.assertEqual(len(self.texts()),1);self.assertIn('Best 2 together',self.texts()[0])
 def test_category_alert_sends_text_then_photo_followup(self):
  self.client();rid=self.cat_alert(once=True,open_on_pc=True);s=self.open_show()
  self.assertEqual(len(self.texts()),1);self.assertIn('Category now available',self.texts()[0])
  [p]=self.pending();self.assertEqual((p['alert_id'],p['show_id'],p['mode'],json.loads(p['categories'])),(rid,s['id'],'photo',['Gold']))
  self.db.execute('UPDATE outbox SET delivered=1');prefs.delivered(self.db,rid);self.db.commit()
  self.assertEqual(prefs.rules(self.db)[0]['status'],'completed')  # the follow-up is still sent for a finished once alert
  self.assertEqual(len(prefs.active(self.db)),0)
  self.read(s,{**result('1','2','3'),'seats':[{'row_id':'Gold / B','seat':str(i),'available':True} for i in range(1,4)]+[{'row_id':'Gold / A','seat':'1','available':False}]})
  self.assertEqual(self.pending(),[])
  rows=self.db.execute('SELECT text,photo,alert_id FROM outbox WHERE delivered=0').fetchall();self.assertEqual(len(rows),1)
  self.assertTrue(rows[0]['text'].startswith('📸 Gold · 3 seats open\n\n🎬 Test Movie\n📍 Test Cinema'));self.assertIn('/seat-layout/',rows[0]['text'])
  self.assertTrue(rows[0]['photo'].startswith(b'\x89PNG'));self.assertEqual(rows[0]['alert_id'],rid)
  self.assertEqual(len(prefs.pc_alerts(self.db,0)['events']),1)  # Open on PC opens the page for the alert, not again for the follow-up
 def test_followup_merges_categories_and_reports_none_open(self):
  rid=self.cat_alert();s=self.open_show()
  prefs.queue_seat_photo(self.db,rid,s['id'],['Gold.'],'photo');[p]=self.pending();self.assertEqual(json.loads(p['categories']),['Gold','Gold.'])
  res={'rows':[{'id':'Gold / B','category':'Gold','row':'B'},{'id':'Gold. / B','category':'Gold.','row':'B'}],
   'available':[{'row_id':'Gold. / B','seat':'1'},{'row_id':'Gold. / B','seat':'2'}],
   'seats':[{'row_id':'Gold / B','seat':'1','available':False},{'row_id':'Gold. / B','seat':'1','available':True},{'row_id':'Gold. / B','seat':'2','available':True}],'url':'x'}
  self.read(s,res);self.assertIn('📸 Gold · 0 seats open · Gold. · 2 seats open\n',self.texts()[-1])  # "Gold." is a different category
  prefs.queue_seat_photo(self.db,rid,s['id'],['Gold'],'photo');self.read(s,{**res,'available':[]})
  self.assertTrue(self.texts()[-1].startswith('📸 No seats open in Gold now'));self.assertEqual(len(self.texts()),3)
 def test_gate_holds_until_minimum_then_sends_once_with_photo(self):
  rid=self.cat_alert(min_seats=3,once=True);s=self.open_show()
  self.assertEqual(self.texts(),[]);[p]=self.pending();self.assertEqual(p['mode'],'gate');self.assertIn('Category now available',p['text'])
  self.assertTrue(prefs.gate_waiting(self.db,rid))
  self.read(s,result('1','2'));self.assertEqual(self.texts(),[]);[p]=self.pending()
  self.assertGreaterEqual(p['next_read'],time.time()+590)  # the alert interval (10 min), not the 1-minute read gap
  self.assertEqual(row_watch.targets(self.db)[0],[]);prefs.delivered(self.db,rid);self.assertEqual(prefs.rules(self.db)[0]['status'],'active')  # a gated once alert never completes
  self.db.execute('UPDATE pending_seat_photos SET next_read=0');self.db.commit()
  self.read(s,result('1','2','3'));self.assertEqual(self.pending(),[])
  rows=self.db.execute('SELECT text,photo FROM outbox').fetchall();self.assertEqual(len(rows),1)
  self.assertTrue(rows[0]['text'].startswith('🎬 Test Movie'));self.assertIn('Category now available',rows[0]['text']);self.assertIn('📸 Gold · 3 seats open',rows[0]['text']);self.assertTrue(rows[0]['photo'])
  self.assertGreater(self.db.execute('SELECT last_alert FROM alerts').fetchone()[0],0)
  self.db.execute('UPDATE outbox SET delivered=1');prefs.delivered(self.db,rid);self.assertEqual(prefs.rules(self.db)[0]['status'],'completed')
 def test_gate_drops_when_category_closes_or_alert_stops(self):
  rid=self.cat_alert(min_seats=3);self.open_show();self.assertEqual(len(self.pending()),1)
  api_watch.ingest(self.db,self.job,self.T.response(status='0'));self.assertEqual(self.pending(),[])  # Gold is no longer bookable
  s=self.open_show('3');self.assertEqual(len(self.pending()),1)
  self.db.execute("UPDATE alerts SET payload=json_set(payload,'$.enabled',json('false'))");prefs.prune_seat_photos(self.db);self.assertEqual(self.pending(),[])
  self.assertEqual(self.texts(),[]);self.assertTrue(s['id'])
 def test_expiry(self):
  rid=self.cat_alert();s=self.open_show();now=time.time();sid=s['id']
  self.assertEqual(len(self.pending()),1)
  prefs.queue_seat_photo(self.db,rid,sid,['Gold'],'photo',now=now-prefs.PHOTO_LIFETIME-5)
  prefs.prune_seat_photos(self.db,now);self.assertEqual(self.pending(),[])  # 30 minutes old
  prefs.queue_seat_photo(self.db,rid,sid,['Gold'],'photo',now=now);prefs.prune_seat_photos(self.db,now+60);self.assertEqual(len(self.pending()),1)
  self.assertEqual(prefs.seat_photo_shows(self.db,now),[sid]);self.assertEqual(prefs.seat_photo_shows(self.db,now-60),[])
  prefs.prune_seat_photos(self.db,now+10*86400);self.assertEqual(self.pending(),[])  # the showtime has started
  prefs.queue_seat_photo(self.db,rid,sid,['Gold'],'photo');self.db.commit();prefs.delete(self.db,rid);self.assertEqual(self.pending(),[])  # alert deleted
  prefs.queue_seat_photo(self.db,'missing',sid,['Gold'],'photo');prefs.prune_seat_photos(self.db);self.assertEqual(self.pending(),[])
 def test_pending_read_is_ranked_first_and_not_a_health_problem(self):
  a=show('A');self.add(a);self.alert();rid=prefs.rules(self.db)[0]['id'];other=show('Z',movie='Other');self.add(other)
  now=time.time();prefs.queue_seat_photo(self.db,rid,'sZ',['Gold'],'photo',now=now-5);self.db.commit()
  row_watch.schedule_active(self.db,now);self.assertEqual(row_watch.next_job(self.db,now)['show_id'],'sZ')
  self.assertIn('sZ',row_watch.queue_order(self.db,now)[2]);self.assertEqual(row_watch.targets(self.db,now)[1],set())
  setmeta(self.db,'health_watch_started',now-9999);setmeta(self.db,'city_heartbeat',now);setmeta(self.db,'row_reader_heartbeat',now)
  self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,checked,error,due) VALUES('sZ','{}',?,'Seat source HTTP 403',0)",(now,));self.db.commit()
  self.assertEqual(health_watch.problems(self.db,now),{})
 def test_seat_source_disabled(self):
  with patch.dict(os.environ,{'BMS_ROWS_ENABLED':'0'}):
   rid=self.cat_alert(min_seats=3);self.open_show()
   self.assertEqual(self.pending(),[]);self.assertEqual(len(self.texts()),1);self.assertTrue(self.texts()[0].endswith('(seat count unavailable)'))
   self.db.execute('DELETE FROM outbox');self.db.execute('UPDATE alerts SET last_alert=0');self.db.commit();prefs.save(self.db,{**prefs.defaults(),'movies':['Test Movie'],'enabled':True,'once':False},rid)
   self.open_show('0','3');self.assertEqual(self.pending(),[]);self.assertNotIn('unavailable',self.texts()[-1])

if __name__=='__main__':unittest.main()
