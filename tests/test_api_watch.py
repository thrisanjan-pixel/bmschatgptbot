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
