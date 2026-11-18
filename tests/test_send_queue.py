import time
import unittest
from unittest.mock import patch
from tests import test_api_watch as fixtures
from store import emit
import telegram
import watch_preferences as prefs


class SendQueueTests(unittest.TestCase):
    """Seat-map messages of one alert go out together, and waiting messages can be cancelled."""
    setUp=fixtures.ApiTests.setUp
    tearDown=fixtures.ApiTests.tearDown
    def waiting(self):return [r[0] for r in self.db.execute('SELECT text FROM outbox WHERE delivered=0 ORDER BY created,rowid')]
    def test_held_message_waits_for_other_showtimes_then_all_go(self):
        prefs.queue_seat_photo(self.db,'a','s2',['Gold'],'photo')  # a second showtime is not read yet
        emit(self.db,'one','map one',alert_id='a',hold=prefs.PHOTO_HOLD);prefs.release_seat_photos(self.db);self.db.commit()
        with patch('telegram.send') as send:self.assertIsNone(telegram.drain(self.db))
        send.assert_not_called()
        self.assertEqual(prefs.pending_deliveries(self.db)[0]['state'],'held')
        emit(self.db,'two','map two',alert_id='a',hold=prefs.PHOTO_HOLD)
        self.db.execute('DELETE FROM pending_seat_photos');prefs.release_seat_photos(self.db);self.db.commit()
        with patch('telegram.send') as send:
            self.assertEqual(telegram.drain(self.db),'a');self.assertEqual(telegram.drain(self.db),'a')
        self.assertEqual([c.args[0] for c in send.call_args_list],['map one','map two'])
    def test_hold_ends_by_itself_and_spares_other_rows(self):
        prefs.queue_seat_photo(self.db,'a','s2',['Gold'],'photo')
        emit(self.db,'one','map one',alert_id='a',hold=prefs.PHOTO_HOLD)
        emit(self.db,'claimed','being sent',alert_id='b');emit(self.db,'retry','retrying',alert_id='b')
        now=time.time()
        self.db.execute("UPDATE outbox SET next_try=? WHERE text='being sent'",(now+120,))
        self.db.execute("UPDATE outbox SET next_try=?,attempts=1 WHERE text='retrying'",(now+900,))
        prefs.release_seat_photos(self.db)  # alert b has no seat read waiting, yet its claimed and retried rows stay hidden
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox WHERE next_try>?',(now,)).fetchone()[0],3)
        # A seat read that never finishes cannot hold the message past PHOTO_HOLD.
        self.assertLessEqual(self.db.execute("SELECT next_try FROM outbox WHERE text='map one'").fetchone()[0],time.time()+prefs.PHOTO_HOLD)
        self.assertLess(prefs.PHOTO_HOLD,600)  # sent before drain would label it a delayed alert
    def test_gate_waiting_for_more_seats_does_not_hold_others(self):
        prefs.queue_seat_photo(self.db,'a','s2',['Gold'],'gate','text')
        self.db.execute('UPDATE pending_seat_photos SET next_read=created+600')  # read once, too few seats
        emit(self.db,'one','map one',alert_id='a',hold=prefs.PHOTO_HOLD);prefs.release_seat_photos(self.db)
        self.assertEqual(self.db.execute('SELECT next_try FROM outbox').fetchone()[0],0)
        self.assertEqual([i['state'] for i in prefs.pending_deliveries(self.db)],['sending','seats'])
    def test_cancel_one_and_all(self):
        emit(self.db,'one','first line\nsecond line',alert_id='a');emit(self.db,'two','other',alert_id='b')
        emit(self.db,'health','system warning',alert_id='__health__')
        prefs.queue_seat_photo(self.db,'a','s1',['Gold'],'photo')
        items=prefs.pending_deliveries(self.db)
        self.assertEqual([(i['title'],i['state']) for i in items],[('first line','sending'),('other','sending'),('Showtime','map')])
        prefs.cancel_deliveries(self.db,items[0]['id']);self.assertEqual(self.waiting(),['other','system warning'])
        prefs.cancel_deliveries(self.db,items[2]['id']);self.assertEqual(self.db.execute('SELECT count(*) FROM pending_seat_photos').fetchone()[0],0)
        with self.assertRaises(ValueError):prefs.cancel_deliveries(self.db,'nonsense')
        prefs.queue_seat_photo(self.db,'a','s1',['Gold'],'photo');prefs.cancel_deliveries(self.db)
        self.assertEqual(self.waiting(),['system warning'])  # health warnings are never cancelled
        self.assertEqual(prefs.pending_deliveries(self.db),[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM suppressed_outbox').fetchone()[0],2)
    def test_cancelling_the_waiting_read_releases_held_messages(self):
        prefs.queue_seat_photo(self.db,'a','s2',['Gold'],'photo')
        emit(self.db,'one','map one',alert_id='a',hold=prefs.PHOTO_HOLD)
        prefs.cancel_deliveries(self.db,'p:a|s2')
        self.assertEqual(self.db.execute('SELECT next_try FROM outbox').fetchone()[0],0)


if __name__=='__main__':unittest.main()
