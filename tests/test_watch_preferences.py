import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch
from tests import test_api_watch as fixtures
response=fixtures.response
DAY=fixtures.DAY
from dashboard import handler
from store import meta,setmeta,emit
from api_watch import ingest
import watch_preferences as prefs

class PreferenceTests(unittest.TestCase):
    setUp=fixtures.ApiTests.setUp
    tearDown=fixtures.ApiTests.tearDown
    def configure(self,**values):
        p={**prefs.defaults(),'movies':['Test Movie'],**values}
        self.db.commit();prefs.save(self.db,p);return p
    def test_paused_and_unmatched_never_queue(self):
        self.configure(enabled=False)
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
        self.configure(enabled=True,movies=['Other movie'])
        ingest(self.db,self.job,response('3'))
        self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],0)
    def test_rate_limit_skips_without_replay(self):
        self.configure(enabled=True)
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'));ingest(self.db,self.job,response('3'))
        self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],1)
        setmeta(self.db,'watch_last_alert','0');ingest(self.db,self.job,response('3'))
        self.assertEqual(self.db.execute('select count(*) from outbox').fetchone()[0],1)
    def test_save_archives_old_queue(self):
        emit(self.db,'old','Old alert')
        self.configure(enabled=False)
        self.assertEqual(self.db.execute('select count(*) from outbox where delivered=0').fetchone()[0],0)
        self.assertEqual(self.db.execute('select count(*) from suppressed_outbox').fetchone()[0],1)
        self.assertEqual(meta(self.db,'notifications_paused'),'1')
    def test_one_time_completion_and_rearm(self):
        self.configure(enabled=True,once=True)
        prefs.delivered(self.db)
        self.assertFalse(prefs.read(self.db)['enabled'])
        self.assertEqual(meta(self.db,'notifications_paused'),'1')
        self.assertGreater(float(meta(self.db,'watch_completed')),0)
        self.configure(enabled=True,once=True)
        self.assertTrue(prefs.read(self.db)['enabled'])
        self.assertEqual(meta(self.db,'watch_completed'),'0')
    def test_repeat_mode_stays_enabled(self):
        self.configure(enabled=True,once=False)
        prefs.delivered(self.db)
        self.assertTrue(prefs.read(self.db)['enabled'])
    def test_delivery_can_preserve_outer_transaction_for_atomic_pause(self):
        from telegram import drain
        self.configure(enabled=True,once=True)
        emit(self.db,'atomic','Test alert');self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        with patch('telegram.send') as send:drain(self.db,commit=False)
        self.assertTrue(self.db.in_transaction)
        send.assert_called_once()
        prefs.delivered(self.db);self.db.rollback()
        self.assertTrue(prefs.read(self.db)['enabled'])
        self.assertEqual(self.db.execute('SELECT delivered FROM outbox').fetchone()[0],0)
    def test_option_profiles_use_actual_cinema_categories(self):
        ingest(self.db,self.job,response())
        profiles=prefs.options(self.db)['profiles']
        self.assertEqual(len(profiles),1)
        self.assertEqual(profiles[0]['cinema'],'TEST')
        self.assertEqual(profiles[0]['categories'],('Gold',))
    def test_all_dimensions(self):
        s={'movie':'Film','venue_code':'PVR','date':DAY,'language':'Telugu • IMAX','format':'','time':'07:30 PM','categories':{'a':{'name':'Recliner'}}}
        p={**prefs.defaults(),'movies':['Film'],'cinemas':['PVR'],'dates':[DAY],'languages':['Telugu'],'formats':['IMAX'],'categories':['Recliner'],'time_from':'19:00','time_to':'21:00'}
        self.assertTrue(prefs.matches(s,p))
        for key in prefs.FIELDS:
            self.assertFalse(prefs.matches(s,{**p,key:['Wrong']}),key)
        self.assertFalse(prefs.matches(s,{**p,'time_to':'19:00'}))
    def test_future_screen_and_format_filters_match_when_showtime_is_added(self):
        show={'movie':'Film','venue_code':'PVR','date':DAY,'language':'Telugu • IMAX 3D','format':'HDR','time':'07:30 PM','categories':{'a':{'name':'Recliner'}}}
        p={**prefs.defaults(),'movies':['Film'],'screens':['HDR'],'formats':['IMAX 3D'],'languages':['Telugu'],'new_shows':True}
        self.assertTrue(prefs.matches(show,p))
        self.assertFalse(prefs.matches(show,{**p,'screens':['PCX']}))
        self.assertTrue(prefs.matches(show,{**p,'formats':['3D']}))
        self.assertFalse(prefs.matches(show,{**p,'formats':['2D']}))
    def test_catalog_movie_is_selectable_before_showtimes_exist(self):
        self.db.execute('INSERT INTO inventory VALUES(?,?,?)',('https://in.bookmyshow.com/movies/hyderabad/film/ET12345678','Upcoming Film',1))
        options=prefs.options(self.db)
        self.assertIn('Upcoming Film',options['movies'])
        self.assertEqual(options['showtimes'],[])
    def test_validate_requires_movie_and_bounded_frequency(self):
        for p in ({'enabled':True},{'interval_minutes':0},{'time_from':'21:00','time_to':'19:00'}):
            with self.assertRaises(ValueError):prefs.validate(p)
    def test_http_csrf_and_persistence(self):
        self.db.commit()
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(self.tmp.name+'/state.sqlite3')))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}/api/watch'
        try:
            with urllib.request.urlopen(url) as r:data=json.load(r)
            for invalid in ([],None,'invalid'):
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(invalid).encode(),headers={'X-CSRF-Token':data['csrf']}))
                self.assertEqual(caught.exception.code,400);caught.exception.close()
            body=json.dumps({**prefs.defaults(),'movies':['Test Movie']}).encode()
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(url,data=body))
            self.assertEqual(e.exception.code,403);e.exception.close()
            with urllib.request.urlopen(urllib.request.Request(url,data=body,headers={'X-CSRF-Token':data['csrf']})) as r:self.assertEqual(r.status,200)
            with urllib.request.urlopen(url) as r:self.assertEqual(json.load(r)['preferences']['movies'],['Test Movie'])
            with urllib.request.urlopen(url+'?new=1') as r:self.assertEqual(json.load(r)['preferences']['movies'],[])
        finally:server.shutdown();server.server_close();thread.join()
