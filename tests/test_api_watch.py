import datetime as dt
import copy
import json
import tempfile
import unittest
from pathlib import Path
from api_source import IST,ReadError,parse,seat_layout_url,show_url
from api_watch import ingest
from store import connect,coverage,setmeta
from watch_preferences import defaults

DAY=(dt.datetime.now(IST)+dt.timedelta(days=2)).strftime('%Y%m%d')
EVENT='ET12345678'
def response(session='1',status='0',event=EVENT):
    return {'data':{'additionalData':{'eventCode':event,'dateCode':DAY},'topStickyWidgets':[],
        'showtimeWidgets':[{'type':'groupList','data':[{'type':'venueGroup','data':[{'type':'venue-card',
        'additionalData':{'venueCode':'TEST','venueName':'Test Cinema'},
        'header':{'redirectionUrl':f'https://in.bookmyshow.com/cinemas/hyderabad/test/buytickets/TEST/{DAY}'},
        'showtimes':[{'title':'11:00 AM','additionalData':{'sessionId':session,'showDateCode':DAY,'showTimeCode':'1100',
        'cutOffDateTime':DAY+'1200','categories':[{'priceCode':'1','priceDesc':'Gold','availStatus':status,'curPrice':'200'}]}}]}]}]}]}}

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(str(Path(self.tmp.name)/'state.sqlite3'))
        setmeta(self.db,'watch_preferences',json.dumps({**defaults(),'enabled':True,'availability':True,'interval_minutes':0}))
        self.job={'id':'discovery:'+show_url(EVENT,DAY),'payload':json.dumps({'event':EVENT,'date':DAY,'movie':'Test Movie','notify_first':False})}
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_silent_baseline_and_new_show(self):
        ingest(self.db,self.job,response());self.assertEqual(coverage(self.db)['pending_alerts'],0)
        ingest(self.db,self.job,response('2'));self.assertEqual(coverage(self.db)['pending_alerts'],1)
    def test_alert_links_match_changed_cinemas(self):
        baseline=response()
        cards=baseline['data']['showtimeWidgets'][0]['data'][0]['data']
        for code,name in [('PVR','PVR Cinema'),('ART','ART Cinemas')]:
            card=copy.deepcopy(cards[0])
            card['additionalData']={'venueCode':code,'venueName':name}
            card['header']['redirectionUrl']=f'https://in.bookmyshow.com/cinemas/hyderabad/{code.lower()}/buytickets/{code}/{DAY}'
            cards.append(card)
        ingest(self.db,self.job,baseline)
        changed=copy.deepcopy(baseline)
        cards=changed['data']['showtimeWidgets'][0]['data'][0]['data']
        cards[1]['showtimes'][0]['additionalData']['sessionId']='2'
        cards[2]['showtimes'][0]['additionalData']['categories'][0]['availStatus']='3'
        ingest(self.db,self.job,changed)
        messages=[r[0] for r in self.db.execute('SELECT text FROM outbox')]
        self.assertEqual(len(messages),2)
        for code,name in [('PVR','PVR Cinema'),('ART','ART Cinemas')]:
            message=next(m for m in messages if name in m)
            session='2' if code=='PVR' else '1'
            self.assertIn(f'/movies/hyderabad/seat-layout/{EVENT}/{code}/{session}/{DAY}',message)
            self.assertNotIn('/buytickets/',message)
            self.assertIn(dt.datetime.strptime(DAY,'%Y%m%d').strftime('%d %b %Y'),message)
        ingest(self.db,self.job,changed)
        self.assertEqual(coverage(self.db)['pending_alerts'],2)
    def test_repeat_category_releases_and_retry_persistence(self):
        ingest(self.db,self.job,response())
        for status in ('3','0','3'):ingest(self.db,self.job,response(status=status))
        self.assertEqual(coverage(self.db)['pending_alerts'],2)
        self.assertIn('Category now available',self.db.execute('SELECT text FROM outbox LIMIT 1').fetchone()[0])
    def test_blocked_or_newly_listed_category_opening_alerts(self):
        ingest(self.db,self.job,response(status='4'))
        ingest(self.db,self.job,response(status='1'))
        self.assertEqual(coverage(self.db)['pending_alerts'],1)
        added=response(status='1');cats=added['data']['showtimeWidgets'][0]['data'][0]['data'][0]['showtimes'][0]['additionalData']['categories']
        cats.append({'priceCode':'2','priceDesc':'Gold.','availStatus':'2','curPrice':'250'})
        ingest(self.db,self.job,added)
        self.assertEqual(coverage(self.db)['pending_alerts'],2)
        self.assertTrue(any('Gold. (₹250)' in r[0] for r in self.db.execute('SELECT text FROM outbox')))
    def test_default_and_explicit_date_do_not_duplicate(self):
        ingest(self.db,self.job,response())
        other=dict(self.job,id='default',payload=json.dumps({'event':EVENT,'date':'','movie':'Test Movie','notify_first':False}))
        ingest(self.db,other,response('2'));ingest(self.db,self.job,response('2'))
        self.assertEqual(coverage(self.db)['pending_alerts'],1)
    def test_failure_preserves_state(self):
        ingest(self.db,self.job,response())
        for bad in (response(status='mystery'),response(event='ET99999999')):
            with self.assertRaises(ReadError):ingest(self.db,self.job,bad)
        self.assertEqual(coverage(self.db)['sessions'],1)
    def test_wrong_city_rejected(self):
        bad=response();bad['data']['showtimeWidgets'][0]['data'][0]['data'][0]['header']['redirectionUrl']='https://in.bookmyshow.com/cinemas/mumbai/test/buytickets/TEST/'+DAY
        with self.assertRaises(ReadError):parse(bad,EVENT,DAY)
    def test_seat_layout_url_targets_exact_showtime(self):
        self.assertEqual(seat_layout_url({'event':EVENT,'venue_code':'TEST','session':'6249','date':DAY}),
            f'https://in.bookmyshow.com/movies/hyderabad/seat-layout/{EVENT}/TEST/6249/{DAY}')
        with self.assertRaises(ReadError):seat_layout_url({'event':EVENT,'venue_code':'TEST','session':'x/evil','date':DAY})
    def test_missing_categories_preserve_last_snapshot(self):
        ingest(self.db,self.job,response())
        bad=response();bad['data']['showtimeWidgets'][0]['data'][0]['data'][0]['showtimes'][0]['additionalData']['categories']=[]
        with self.assertRaises(ReadError):ingest(self.db,self.job,bad)
        self.assertEqual(coverage(self.db)['sessions'],1)


class ThrottleTests(unittest.TestCase):
    setUp=ApiTests.setUp
    tearDown=ApiTests.tearDown
    def test_one_ingest_delivers_every_cinema_and_interval_defers_the_next(self):
        from watch_preferences import save
        save(self.db,{**defaults(),'movies':['Test Movie'],'enabled':True,'once':False,'interval_minutes':10})
        base=response();cards=base['data']['showtimeWidgets'][0]['data'][0]['data']
        for code in ('PVR','ART'):
            card=copy.deepcopy(cards[0]);card['additionalData']={'venueCode':code,'venueName':code+' Cinema'}
            card['header']['redirectionUrl']=f'https://in.bookmyshow.com/cinemas/hyderabad/{code.lower()}/buytickets/{code}/{DAY}';cards.append(card)
        ingest(self.db,self.job,base)
        changed=copy.deepcopy(base);cs=changed['data']['showtimeWidgets'][0]['data'][0]['data']
        cs[1]['showtimes'][0]['additionalData']['sessionId']='2';cs[2]['showtimes'][0]['additionalData']['sessionId']='3'
        ingest(self.db,self.job,changed)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
        later=copy.deepcopy(changed);later['data']['showtimeWidgets'][0]['data'][0]['data'][0]['showtimes'][0]['additionalData']['sessionId']='4'
        self.db.execute('UPDATE outbox SET delivered=1');self.db.commit()
        ingest(self.db,self.job,later)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
        self.db.execute('UPDATE alerts SET last_alert=0');ingest(self.db,self.job,later)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],3)


class UpcomingTests(unittest.TestCase):
    setUp=ApiTests.setUp
    tearDown=ApiTests.tearDown
    def test_event_leaves_upcoming_mode_once_a_show_is_bookable(self):
        setmeta(self.db,'upcoming_event:'+EVENT,'1');setmeta(self.db,'waiting_show:'+self.job['id'],'1')
        texts=lambda:[r[0] for r in self.db.execute('SELECT text FROM outbox ORDER BY rowid')]
        ingest(self.db,self.job,response('1','1'))
        self.assertEqual(len(texts()),1);self.assertIn('Booking open',texts()[0])
        self.assertEqual(self.db.execute("SELECT count(*) FROM meta WHERE key=?",('upcoming_event:'+EVENT,)).fetchone()[0],0)
        ingest(self.db,self.job,response('1','1'));self.assertEqual(len(texts()),1)
        later=response('1','1');shows=later['data']['showtimeWidgets'][0]['data'][0]['data'][0]['showtimes'];shows.append(copy.deepcopy(shows[0]));shows[1]['additionalData']['sessionId']='2'
        ingest(self.db,self.job,later)
        self.assertEqual(len(texts()),2);self.assertIn('New show',texts()[1]);self.assertNotIn('Booking open',texts()[1])


def test_wrong_date_retries_only_the_affected_listing(tmp_path):
    import json
    import api_source
    import api_watch
    from contextlib import closing
    from store import connect, meta, setmeta

    with closing(connect(str(tmp_path/'state.sqlite3'))) as db:
        api_watch.schedule(db,EVENT,DAY,'Movie')
        job=db.execute('SELECT * FROM jobs').fetchone()
        data=response()
        data['data']['additionalData']['dateCode']='20000101'
        db.execute('INSERT INTO snapshots(id,payload) VALUES(?,?)',('existing',json.dumps({'kept':True})))
        setmeta(db,'source_cooldown','2000')
        try:
            api_source.parse(data,EVENT,DAY)
        except api_source.WrongDateError as error:
            assert error.unlisted
            data['data']['additionalData']['eventCode']='ET99999999'
            try:api_source.parse(data,EVENT,DAY)
            except api_source.WrongDateError as other:error=other
            assert not error.unlisted
            assert api_watch.record_listing_failure(db,job,error,990,1000)=='BookMyShow answered for another event or an invalid date'
        else:
            raise AssertionError('Expected the mismatched response to be rejected')
        assert tuple(db.execute('SELECT due,checked,error FROM jobs').fetchone())==(1300,0,'BookMyShow answered for another event or an invalid date')
        assert meta(db,'source_cooldown')=='2000'
        assert json.loads(db.execute('SELECT payload FROM snapshots').fetchone()[0])=={'kept':True}
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
