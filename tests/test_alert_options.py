"""Each alert option alerts the right showtime and ignores the wrong one."""
import copy,json,os,time,unittest
from unittest.mock import patch
from api_watch import ingest
from store import setmeta
import row_watch
import watch_preferences as prefs
from tests import test_api_watch as fixtures
from tests.test_api_watch import response,DAY,EVENT
from api_source import parse

def show(**v):
    base={'movie':'Test Movie','venue_code':'TEST','venue':'Test Cinema','date':DAY,'language':'Telugu • 2D','format':'PCX SCREEN','time':'07:30 PM',
        'categories':{'1':{'name':'Gold','status':'3','price':''},'2':{'name':'Silver','status':'0','price':''}}}
    return {**base,**v}
def pref(**v):return {**prefs.defaults(),'movies':['Test Movie'],**v}
def count(db,alert=None):
    return db.execute('SELECT count(*) FROM outbox'+(' WHERE alert_id=?' if alert else ''),(alert,) if alert else ()).fetchone()[0]

class MatchTests(unittest.TestCase):
    def test_aot_catalogue_name_and_listing_title_use_the_same_filters(self):
        s=show(movie='AOT - Aakasamlo Oka Tara',venue_code='ALUC',
               language='Telugu • DOLBY CINEMA 2D',format='DOLBY CINEMA',time='07:50PM')
        alert=pref(movies=['Aakasamlo Oka Tara'],cinemas=['ALUC'],screens=['DOLBY CINEMA'],
                   languages=['Telugu'],formats=['2D'],dates=[DAY],categories=['Gold'],
                   time_from='19:50',time_to='19:50')
        self.assertTrue(prefs.matches(s,alert))
        for key,value in [('movies',['AOT']),('screens',['IMAX']),('languages',['Tamil']),
                          ('formats',['3D']),('categories',['GOLD.']),('time_from','19:51')]:
            self.assertFalse(prefs.matches(s,{**alert,key:value}),key)
    def test_each_filter_accepts_right_show_and_rejects_wrong_one(self):
        s=show()
        for key,good,bad in [('movies',['  test   MOVIE '],['Other']),('cinemas',['OTHER','TEST'],['OTHER']),('screens',['pcx screen'],['IMAX']),
            ('languages',['telugu'],['Hindi']),('formats',['2D'],['3D']),('dates',['20000101',DAY],['20000101']),('categories',['gold'],['Platinum'])]:
            self.assertTrue(prefs.matches(s,pref(**{key:good})),key)
            self.assertFalse(prefs.matches(s,pref(**{key:bad})),key)
    def test_time_window_and_empty_values(self):
        s=show()
        self.assertTrue(prefs.matches(s,pref()))
        self.assertTrue(prefs.matches(s,pref(time_from='19:30',time_to='19:30')))
        self.assertTrue(prefs.matches(s,pref(time_from='19:00')));self.assertTrue(prefs.matches(s,pref(time_to='20:00')))
        self.assertFalse(prefs.matches(s,pref(time_from='19:31')));self.assertFalse(prefs.matches(s,pref(time_to='19:29')))
        self.assertTrue(prefs.matches(show(time='12:05 AM'),pref(time_from='00:00',time_to='01:00')))
        self.assertTrue(prefs.matches(show(time='12:05 PM'),pref(time_from='12:00',time_to='13:00')))
        self.assertFalse(prefs.matches(show(time=''),pref(time_from='10:00')))
    def test_midnight_crossing_range_is_rejected_not_silently_never_matching(self):
        with self.assertRaises(ValueError):prefs.validate(pref(time_from='22:00',time_to='02:00'))
        self.assertEqual(prefs.validate(pref(time_from='',time_to=''))['time_from'],'')

class IngestOptionTests(unittest.TestCase):
    tearDown=fixtures.ApiTests.tearDown
    def setUp(self):
        fixtures.ApiTests.setUp(self);setmeta(self.db,"watch_preferences","{}");self.db.commit()  # no fixture legacy alert
    def alert(self,**v):return prefs.save(self.db,pref(enabled=True,once=False,interval_minutes=10,**v),create=True)
    def test_catalogue_title_schedules_and_primes_prefixed_listing(self):
        from api_watch import is_watched,wanted_dates
        data=response(status='3')
        ingest(self.db,self.job,data);self.db.commit()
        raw=self.db.execute('SELECT id,payload FROM sessions').fetchone()
        details=json.loads(raw['payload'])
        details.update(movie='AOT - Aakasamlo Oka Tara',language='Telugu • DOLBY CINEMA 2D',format='DOLBY CINEMA')
        self.db.execute('UPDATE sessions SET payload=? WHERE id=?',(json.dumps(details),raw['id']))
        self.db.commit()
        alert=self.alert(movies=['Aakasamlo Oka Tara'],cinemas=['TEST'],screens=['DOLBY CINEMA'],
                         languages=['Telugu'],formats=['2D'],dates=[DAY],categories=['Gold'])
        payload={'movie':'AOT - Aakasamlo Oka Tara','event':EVENT,'date':DAY}
        self.assertTrue(is_watched(payload,wanted_dates(self.db)))
        payload['date']='20000101'
        self.assertFalse(is_watched(payload,wanted_dates(self.db)))
        prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,alert),1)

    def test_matching_filters_with_different_minimums_each_queue_verification(self):
        ingest(self.db,self.job,response(status='3'));self.db.commit()
        first=self.alert(min_seats=6)
        second=self.alert(min_seats=12)
        with patch.dict(os.environ,{'BMS_ROWS_ENABLED':'1'}):
            prefs.prime_availability(self.db)
            self.assertTrue(prefs.gate_waiting(self.db,first))
            self.assertTrue(prefs.gate_waiting(self.db,second))

    def test_screen_cinema_and_category_only_alert_the_matching_show(self):
        good=self.alert(cinemas=['TEST'],categories=['Gold'],dates=[DAY]);bad=self.alert(cinemas=['NOPE']);other=self.alert(categories=['Platinum'])
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual((count(self.db,good),count(self.db,bad),count(self.db,other)),(1,0,0))
    def test_new_shows_off_only_alerts_availability_and_vice_versa(self):
        new=self.alert(availability=False);avail=self.alert(new_shows=False)
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual((count(self.db,new),count(self.db,avail)),(1,0))
        ingest(self.db,self.job,response('2','3'))
        self.assertEqual((count(self.db,new),count(self.db,avail)),(1,1))
    def test_movie_name_spelling_and_case(self):
        a=self.alert(movies=['  tEsT   movie'])
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual(count(self.db,a),1)
    def test_several_cinemas_and_dates_in_one_alert(self):
        a=self.alert(cinemas=['NOPE','TEST'],dates=['20000101',DAY])
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual(count(self.db,a),1)
    def test_preferred_time_filters_new_show(self):
        late=self.alert(time_from='12:00');early=self.alert(time_from='10:00',time_to='12:00')
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual((count(self.db,late),count(self.db,early)),(0,1))
    def test_paused_and_deleted_and_cancelled_alerts_stay_silent(self):
        paused=self.alert();gone=self.alert();cancelled=self.alert()
        prefs.save(self.db,pref(enabled=False),paused);prefs.delete(self.db,gone);prefs.transition(self.db,cancelled,'cancel')
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.assertEqual(count(self.db),0)
    def test_once_completes_after_first_delivery_repeat_keeps_running(self):
        one=prefs.save(self.db,pref(enabled=True,once=True),create=True);many=self.alert()
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.db.execute('UPDATE outbox SET delivered=1')  # delivered() runs after a message is sent
        prefs.delivered(self.db,one);prefs.delivered(self.db,many)
        self.assertEqual([r['id'] for r in prefs.active(self.db)],[many])
        self.assertEqual(next(r for r in prefs.rules(self.db) if r['id']==one)['status'],'completed')
    def test_interval_gap_blocks_next_alert_until_it_passes(self):
        a=self.alert();ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'))
        self.db.execute('UPDATE outbox SET delivered=1');self.db.commit()
        ingest(self.db,self.job,response('3'));self.assertEqual(count(self.db),1)
        self.db.execute('UPDATE alerts SET last_alert=?',(time.time()-601,));ingest(self.db,self.job,response('3'))
        self.assertEqual(count(self.db,a),2)
    def test_future_only_notify_existing_skips_open_seats_now_but_alerts_new_opening(self):
        ingest(self.db,self.job,response(status='3'));self.db.commit()
        now=self.alert(new_shows=False,notify_existing=True);future=self.alert(new_shows=False,notify_existing=False)
        prefs.prime_availability(self.db)
        self.assertEqual((count(self.db,now),count(self.db,future)),(1,0))
        ingest(self.db,self.job,response(status='0'));ingest(self.db,self.job,response(status='3'))
        self.assertEqual(count(self.db,future),1)
    def test_two_identical_alerts_send_one_already_available_message(self):
        # Both alerts post to the same Telegram chat, so one message covers both.
        ingest(self.db,self.job,response(status='3'));self.db.commit()
        a=self.alert(new_shows=False);b=self.alert(new_shows=False);prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,a)+count(self.db,b),1)
    def test_initial_availability_retries_after_listing_inventory_arrives(self):
        alert=self.alert(new_shows=False,notify_existing=True)
        prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,alert),0)
        ingest(self.db,self.job,response(status='3'))
        prefs.prime_availability(self.db)
        prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,alert),1)
    def test_reset_queues_a_new_notification_for_seats_that_are_still_open(self):
        ingest(self.db,self.job,response(status='3'));self.db.commit()
        alert=prefs.save(self.db,pref(enabled=True,new_shows=False,notify_existing=True,once=True),create=True)
        prefs.prime_availability(self.db)
        self.db.execute('UPDATE outbox SET delivered=1 WHERE alert_id=?',(alert,))
        prefs.delivered(self.db,alert)
        self.db.commit()
        prefs.transition(self.db,alert,'reset')
        prefs.prime_availability(self.db)
        prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,alert),2)
    def test_reset_requeues_minimum_seat_verification(self):
        ingest(self.db,self.job,response(status='3'));self.db.commit()
        alert=prefs.save(self.db,pref(enabled=True,new_shows=False,notify_existing=True,once=True,min_seats=6),create=True)
        with patch.dict(os.environ,{'BMS_ROWS_ENABLED':'1'}):
            prefs.prime_availability(self.db)
            self.assertTrue(prefs.gate_waiting(self.db,alert))
            self.db.execute('DELETE FROM pending_seat_photos WHERE alert_id=?',(alert,))
            self.db.commit()
            prefs.transition(self.db,alert,'reset')
            prefs.prime_availability(self.db)
            prefs.prime_availability(self.db)
            self.assertTrue(prefs.gate_waiting(self.db,alert))
            self.assertEqual(self.db.execute('SELECT count(*) FROM pending_seat_photos WHERE alert_id=?',(alert,)).fetchone()[0],1)
    def test_disappeared_show_is_deactivated_and_silent(self):
        a=self.alert(availability=False)
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('2'));self.assertEqual(count(self.db,a),1)
        ingest(self.db,self.job,response('2'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions WHERE active=1').fetchone()[0],1)
        self.assertEqual(count(self.db,a),1)
    def test_expired_showtime_is_not_offered_or_primed(self):
        ingest(self.db,self.job,response(status='3'))
        self.db.execute('UPDATE sessions SET cutoff=1');self.db.commit()
        a=self.alert(new_shows=False);prefs.prime_availability(self.db)
        self.assertEqual(count(self.db,a),0);self.assertEqual(prefs.options(self.db)['showtimes'],[])

class RowOptionTests(unittest.TestCase):
    tearDown=fixtures.ApiTests.tearDown
    def setUp(self):
        fixtures.ApiTests.setUp(self);setmeta(self.db,"watch_preferences","{}");self.db.commit()
        self.show=next(iter(parse(response(),EVENT,DAY)['shows'].values()));self.show['movie']='Test Movie'
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(self.show['id'],json.dumps(self.show),self.show['cutoff']));self.db.commit();row_watch.request(self.db,self.show['id'])
        self.rows=[{'id':'Gold / A','category':'Gold','row':'A'},{'id':'Gold / B','category':'Gold','row':'B'}]
    def read(self,*seats):
        row_watch.ingest(self.db,self.show,{'rows':copy.deepcopy(self.rows),'available':[{'row_id':r,'seat':s} for r,s in seats],'url':self.show['url']})
    def alert(self,rows,**v):
        return prefs.save(self.db,pref(row_show=self.show['id'],rows=rows,enabled=True,once=False,interval_minutes=10,**v),create=True)
    def test_only_the_selected_row_alerts_and_existing_seats_are_gated(self):
        self.read();self.read(('Gold / A','1'),('Gold / B','1'))
        a=self.alert(['Gold / A']);fut=self.alert(['Gold / A'],notify_existing=False);other=self.alert(['Gold / B'])
        self.read(('Gold / A','1'));self.assertEqual((count(self.db,a),count(self.db,fut),count(self.db,other)),(1,0,0))
        self.read();self.db.execute('UPDATE outbox SET delivered=1');self.db.execute('UPDATE alerts SET last_alert=0');self.read(('Gold / A','2'))
        # Same show and rows: one message covers both alerts (they share a Telegram chat).
        self.assertEqual(count(self.db,a)+count(self.db,fut),2)
    def test_row_alert_respects_interval_and_ignores_listing_changes(self):
        self.read();a=self.alert(['Gold / A'])
        self.read(('Gold / A','1'));self.read();self.read(('Gold / A','2'));self.assertEqual(count(self.db,a),1)
        ingest(self.db,self.job,response());ingest(self.db,self.job,response('9'));self.assertEqual(count(self.db,a),1)
    def test_rows_require_a_row_show_and_a_matching_row_filter(self):
        with self.assertRaises(ValueError):prefs.validate(pref(rows=['Gold / A']))
        with self.assertRaises(ValueError):prefs.validate(pref(row_show=self.show['id']))
        self.read(('Gold / A','1'))
        with self.assertRaises(ValueError):self.alert(['Gold / A'],categories=['Silver'])

if __name__=='__main__':unittest.main()
