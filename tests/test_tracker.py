import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from city_watch import Runtime, add_venue, ingest_venue
from dashboard import status
from parsers import IST, cinema_links, day_end, movie_pages, offered_dates, parse_venue, show_data, today
from seats import parse_cells
from source import SourceError, native, state
from store import connect, coverage, enqueue, meta, setmeta, snapshot
from telegram import DeliveryError, drain


DAY=(dt.datetime.now(IST)+dt.timedelta(days=3)).strftime('%Y%m%d')
BASE='https://in.bookmyshow.com/cinemas/hyderabad/test/buytickets/TEST'


def document(data):
    return '<script>window.__INITIAL_STATE__ = '+json.dumps(data)+';</script>'


def venue_data(session='1',available='2'):
    return {'showDetailsTransformed':{'Date':DAY,'Venues':{'VenueCode':'TEST','VenueName':'Test Cinema'},
            'Event':[{'EventTitle':'Movie','ChildEvents':[{'EventCode':'ET123','EventLanguage':'Telugu',
            'EventDimension':'2D','ShowTimes':[{'SessionId':session,'AvailStatus':available,
            'CutOffDateTime':DAY+'2300','ShowTime':'11:00 PM'}]}]}]},
            'ShowDatesArray':[{'DateCode':DAY,'isDisabled':False}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'state.sqlite3');self.db=connect(self.path)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_queue_refresh_preserves_fair_scheduling(self):
        enqueue(self.db,'venue',BASE,{'v':1});self.db.execute('UPDATE jobs SET due=123,checked=12,error=\'failed\'')
        enqueue(self.db,'venue',BASE,{'v':2});row=self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual((row['due'],row['checked'],row['error']),(123,12,'failed'))
        self.assertEqual(json.loads(row['payload']),{'v':2})
    def test_initial_seats_silent_then_added_in_partial_row(self):
        a={'EX/B/1':{'row':'B','seat':'1'}};b=dict(a,**{'EX/B/2':{'row':'B','seat':'2'}})
        snapshot(self.db,'s',a,lambda added:','.join(added));self.assertEqual(coverage(self.db)['pending_alerts'],0)
        snapshot(self.db,'s',b,lambda added:','.join(added));snapshot(self.db,'s',b,lambda added:'duplicate')
        self.assertEqual(coverage(self.db)['pending_alerts'],1)
        self.assertEqual(self.db.execute('SELECT text FROM outbox').fetchone()[0],'EX/B/2')
    def test_repeated_release_after_unavailability_alerts_again(self):
        for value in ({},{'B1':{}},{},{'B1':{}}):snapshot(self.db,'s',value,lambda added:'release')
        self.assertEqual(coverage(self.db)['pending_alerts'],2)
    def test_failed_delivery_keeps_durable_outbox_and_retries(self):
        snapshot(self.db,'s',{},str);snapshot(self.db,'s',{'B1':{}},str);self.db.commit()
        with patch('telegram.send',side_effect=DeliveryError(429,60)):drain(self.db)
        row=self.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['delivered'],0);self.assertEqual(row['attempts'],1);self.assertGreater(row['next_try'],time.time()+50)
        self.db.execute('UPDATE outbox SET next_try=0');self.db.commit()
        with patch('telegram.send') as send:drain(self.db);send.assert_called_once()
        self.assertEqual(coverage(self.db)['pending_alerts'],0)
    def test_restart_retains_snapshot_and_pending_delivery(self):
        snapshot(self.db,'s',{},str);snapshot(self.db,'s',{'B1':{}},str);self.db.commit();self.db.close();self.db=connect(self.path)
        snapshot(self.db,'s',{'B1':{}},str);self.assertEqual(coverage(self.db)['pending_alerts'],1)
    def make_venue(self):
        add_venue(self.db,{'code':'TEST','name':'Test','url':BASE},DAY)
        return self.db.execute("SELECT * FROM jobs WHERE kind='venue'").fetchone()
    def test_initial_show_baseline_then_new_show(self):
        job=self.make_venue();ingest_venue(self.db,job,venue_data());self.assertEqual(coverage(self.db)['pending_alerts'],0)
        self.db.execute('UPDATE venues SET initialized=1');ingest_venue(self.db,job,venue_data('2'))
        self.assertEqual(coverage(self.db)['pending_alerts'],1)
        self.assertEqual(self.db.execute('SELECT active FROM sessions WHERE id LIKE ?',(f'TEST:{DAY}:1:%',)).fetchone()[0],0)
    def test_new_venue_after_initial_city_baseline_alerts(self):
        setmeta(self.db,'city_initialized','1');job=self.make_venue();ingest_venue(self.db,job,venue_data())
        self.assertEqual(coverage(self.db)['pending_alerts'],1)
    def test_unavailable_summary_never_erases_real_seats(self):
        job=self.make_venue();key=f'seats:TEST:{DAY}:1:ET123'
        snapshot(self.db,key,{'EX/B/1':{}},str)
        ingest_venue(self.db,job,venue_data(available='0'))
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM snapshots WHERE id=?',(key,)).fetchone()[0]),{'EX/B/1':{}})
    def test_all_source_offered_dates_are_queued(self):
        job=self.make_venue();data=venue_data();future=(dt.datetime.now(IST)+dt.timedelta(days=100)).strftime('%Y%m%d')
        data['ShowDatesArray'].append({'DateCode':future,'isDisabled':False});ingest_venue(self.db,job,data)
        self.assertTrue(self.db.execute('SELECT 1 FROM jobs WHERE url=?',(BASE+'/'+future,)).fetchone())
    def test_expired_jobs_excluded_from_coverage(self):
        enqueue(self.db,'seat',BASE,{},time.time()-1);self.assertEqual(coverage(self.db)['seat']['total'],0)
    def test_dashboard_stale_and_no_credentials(self):
        setmeta(self.db,'city_heartbeat',time.time()-200);setmeta(self.db,'city_state','running');self.db.commit()
        result=status(self.path);self.assertEqual(result['state'],'stale')
        self.assertNotIn('TOKEN',json.dumps(result));self.assertNotIn('chat_id',json.dumps(result))
    def test_source_error_preserves_successful_snapshot(self):
        job=self.make_venue();ingest_venue(self.db,job,venue_data());before=self.db.execute('SELECT count(*) FROM sessions').fetchone()[0]
        data=venue_data();data['showDetailsTransformed']['Venues']['VenueCode']='OTHER'
        with self.assertRaises(SourceError):ingest_venue(self.db,job,data)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0],before)


class ParserTests(unittest.TestCase):
    def test_exact_source_host_and_tls(self):
        for url in ['http://in.bookmyshow.com/a','https://in.bookmyshow.com.evil.test/a','https://example.com']:
            with self.assertRaises(SourceError):native(url)
    def test_missing_initial_state_is_failure(self):
        with self.assertRaises(SourceError):state('<h1>Challenge</h1>')
    def test_cutoff_and_venue_validation(self):
        data=venue_data();shows=parse_venue(data,'TEST',DAY,BASE,now=1);self.assertEqual(len(shows),1)
        self.assertEqual(parse_venue(data,'TEST',DAY,BASE,now=day_end(DAY)),[])
        with self.assertRaises(SourceError):parse_venue(data,'OTHER',DAY,BASE)
        with self.assertRaises(SourceError):parse_venue(data,'TEST','20000101',BASE)
    def test_unknown_cutoff_fails_instead_of_claiming_available(self):
        data=venue_data();data['showDetailsTransformed']['Event'][0]['ChildEvents'][0]['ShowTimes'][0]['CutOffDateTime']='unknown'
        with self.assertRaises(SourceError):parse_venue(data,'TEST',DAY,BASE)
    def test_unknown_seat_status_fails(self):
        with self.assertRaises(SourceError):parse_cells([['1','Mystery']],'EX','B')
        self.assertEqual(parse_cells([['1','Booked'],['2','Available']],'EX','B'),{'EX/B/2':{'row':'B','seat':'2'}})
    def test_no_arbitrary_venue_slugs(self):
        self.assertEqual(cinema_links({'name':'Some Cinema','code':'CIN'}),[])
        self.assertEqual(cinema_links({'url':BASE})[0]['code'],'TEST')
        self.assertEqual(cinema_links({'url':BASE.replace('/hyderabad/','/mumbai/')}),[])
    def test_disabled_dates_excluded(self):
        self.assertEqual(offered_dates([{'DateCode':DAY,'isDisabled':True}]),[])
        self.assertEqual(offered_dates([{'DateCode':DAY,'isDisabled':False}]),[DAY])
    def test_movie_all_variants_and_identity(self):
        data={'synopsisMoviesApi':{'queries':{'q':{'status':'fulfilled','originalArgs':{'eventcode':'ET123','regionCode':'HYD'},
          'data':{'meta':{'event':{'eventCode':'ET123','eventName':'Movie'}},'pageCta':[{'ctaType':'bookTickets','isEnabled':True,
          'meta':{'options':[{'formats':[{'eventCode':'ET123'},{'eventCode':'ET456'},{'eventCode':'*'}]}]}}]}}}}}
        url='https://in.bookmyshow.com/movies/hyderabad/movie/ET123'
        self.assertEqual({p['event'] for p in movie_pages(url,document(data))},{'ET123','ET456'})
        data['synopsisMoviesApi']['queries']['q']['originalArgs']['regionCode']='BANG'
        with self.assertRaises(SourceError):movie_pages(url,document(data))
    def test_discovery_event_prefix_mismatch_rejected(self):
        doc=document({'showtimesFunctionalApi':{'queries':{'q':{'status':'fulfilled','originalArgs':{
            'etCodes':'ET1234','dateCode':DAY,'regionCode':'HYD'},'data':{}}}}})
        with self.assertRaises(SourceError):show_data(doc,'discovery','ET123',DAY)


if __name__=='__main__':unittest.main()
