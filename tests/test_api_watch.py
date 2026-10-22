import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from api_source import IST,ReadError,parse,show_url
from api_watch import ingest
from store import connect,coverage

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
        self.job={'id':'discovery:'+show_url(EVENT,DAY),'payload':json.dumps({'event':EVENT,'date':DAY,'movie':'Test Movie','notify_first':False})}
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_silent_baseline_and_new_show(self):
        ingest(self.db,self.job,response());self.assertEqual(coverage(self.db)['pending_alerts'],0)
        ingest(self.db,self.job,response('2'));self.assertEqual(coverage(self.db)['pending_alerts'],1)
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
    def test_missing_categories_preserve_last_snapshot(self):
        ingest(self.db,self.job,response())
        bad=response();bad['data']['showtimeWidgets'][0]['data'][0]['data'][0]['showtimes'][0]['additionalData']['categories']=[]
        with self.assertRaises(ReadError):ingest(self.db,self.job,bad)
        self.assertEqual(coverage(self.db)['sessions'],1)
