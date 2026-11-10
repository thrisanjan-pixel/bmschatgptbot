import json
import sys
import time
import unittest
from unittest.mock import patch
from tests import test_api_watch as fixtures
from store import meta,setmeta,emit
import telegram
import health_watch
import watch_preferences as prefs

class DeliveryFixTests(unittest.TestCase):
    setUp=fixtures.ApiTests.setUp
    tearDown=fixtures.ApiTests.tearDown
    def alert(self,**values):
        self.db.commit();prefs.save(self.db,{**prefs.defaults(),'movies':['Test Movie'],'enabled':True,**values},create=True)
        return prefs.rules(self.db)[0]['id']
    def test_once_alert_completes_only_after_last_message(self):
        a=self.alert(once=True)
        for i in range(3):emit(self.db,f'm{i}',f'message {i}',alert_id=a)
        self.db.commit()
        for i in range(3):
            with patch('telegram.send') as send:got=telegram.drain(self.db)
            self.assertEqual(got,a);self.assertEqual(send.call_count,1)
            prefs.delivered(self.db,got);self.db.commit()
            self.assertEqual(prefs.read(self.db,a)['enabled'],i<2)
        self.assertEqual(self.db.execute('select count(*) from outbox where delivered>0').fetchone()[0],3)
        self.assertEqual(self.db.execute('select count(*) from suppressed_outbox').fetchone()[0],0)
        self.assertEqual(meta(self.db,'notifications_paused'),'1')
    def test_claim_blocks_second_drain(self):
        emit(self.db,'x','hello',alert_id='a');self.db.commit()
        row=self.db.execute('select * from outbox').fetchone();now=time.time()
        self.assertTrue(telegram.claim(self.db,row,now))
        self.assertFalse(telegram.claim(self.db,row,now))
        self.assertGreater(self.db.execute('select next_try from outbox').fetchone()[0],now+100)
        with patch('telegram.send') as send:self.assertIsNone(telegram.drain(self.db))
        send.assert_not_called()
    def test_drain_claims_row_before_sending(self):
        emit(self.db,'x','hello',alert_id='a');self.db.commit()
        seen=[]
        def fake(text,photo=None):seen.append(self.db.execute('select next_try from outbox').fetchone()[0])
        with patch('telegram.send',side_effect=fake):telegram.drain(self.db)
        self.assertGreater(seen[0],time.time()+100)
    def test_429_blocks_all_rows(self):
        for i in range(3):emit(self.db,f'm{i}',f'message {i}',alert_id='a')
        self.db.commit()
        with patch('telegram.send',side_effect=telegram.DeliveryError(429,300)) as send:telegram.drain(self.db)
        self.assertEqual(send.call_count,1)
        self.assertGreater(float(meta(self.db,'telegram_blocked_until')),time.time()+250)
        with patch('telegram.send') as send:self.assertIsNone(telegram.drain(self.db))
        send.assert_not_called()
        setmeta(self.db,'telegram_blocked_until','0');self.db.commit()
        with patch('telegram.send') as send:telegram.drain(self.db)
        self.assertEqual(send.call_count,1)
    def test_date_and_time_formats(self):
        for bad in ({'dates':['2026111']},{'dates':['20261231 ']},{'time_from':'9:30'},{'time_to':'0930'}):
            with self.assertRaises(ValueError):prefs.validate({**prefs.defaults(),**bad})
        ok=prefs.validate({**prefs.defaults(),'dates':['20261231'],'time_from':'09:30','time_to':'23:00'})
        self.assertEqual((ok['dates'],ok['time_from']),(['20261231'],'09:30'))
    def test_options_survives_bad_event_code(self):
        good={**json.loads('{"id":"1","movie":"M","venue_code":"V","venue":"V","date":"20990101","time":"11:00 AM","url":"u","categories":{}}')}
        bad={**good,'id':'2','event':'ET1'}
        for s in (good,bad):self.db.execute('insert into sessions(id,payload,cutoff,active) values(?,?,?,1)',(s['id'],json.dumps(s),time.time()+999))
        self.db.commit()
        with patch('api_source.seat_layout_url',side_effect=lambda s:(_ for _ in ()).throw(ValueError('bad')) if s['id']=='2' else 'http://ok'):
            shows=prefs.options(self.db)['showtimes']
        self.assertEqual({s['id']:s['booking_url'] for s in shows},{'1':'http://ok','2':''})
    def test_health_started_reset_on_start(self):
        setmeta(self.db,'health_watch_started','1');setmeta(self.db,'city_heartbeat','1');self.db.commit()
        self.alert(once=False)
        self.assertIn('monitor',health_watch.problems(self.db,time.time()))
        with patch.object(sys,'argv',['health_watch.py','--db',self.db.execute('PRAGMA database_list').fetchone()[2]]),\
             patch('health_watch.check',side_effect=SystemExit),patch('telegram.drain'),patch('local_credentials.load'):
            with self.assertRaises(SystemExit):health_watch.main()
        self.assertGreater(float(meta(self.db,'health_watch_started')),time.time()-60)
        self.assertEqual(health_watch.problems(self.db,time.time()),{})

if __name__=='__main__':unittest.main()
