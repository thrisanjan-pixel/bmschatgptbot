"""Fixes from the full-bot audit (24 Nov 2026)."""
import copy, json, tempfile, time, unittest
from pathlib import Path

import watch_preferences as prefs
from api_source import show_url
from api_watch import ingest
from store import connect
from tests.test_api_watch import DAY, EVENT, response


class SharedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.db = connect(str(Path(self.tmp.name) / 's.db'))
        base = {**prefs.defaults(), 'movies': ['Test Movie'], 'enabled': True, 'availability': True, 'new_shows': False, 'once': False}
        self.a = prefs.save(self.db, {**base, 'interval_minutes': 10, 'cinemas': ['TEST']}, create=True)
        self.b = prefs.save(self.db, {**base, 'interval_minutes': 30, 'cinemas': ['TEST2']}, create=True)
        self.job = {'id': 'discovery:' + show_url(EVENT, DAY),
                    'payload': json.dumps({'event': EVENT, 'date': DAY, 'movie': 'Test Movie', 'notify_first': False})}

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def page(self, s1, s2, s9='0'):
        r = response(status=s1);cards = r['data']['showtimeWidgets'][0]['data'][0]['data']
        c = copy.deepcopy(cards[0]);c['additionalData'] = {'venueCode': 'TEST2', 'venueName': 'Two'}
        c['header']['redirectionUrl'] = f'https://in.bookmyshow.com/cinemas/hyderabad/two/buytickets/TEST2/{DAY}'
        c['showtimes'][0]['additionalData']['categories'][0]['availStatus'] = s2;cards.append(c)
        s = copy.deepcopy(cards[0]['showtimes'][0]);s['additionalData']['sessionId'] = '9'
        s['additionalData']['categories'][0]['availStatus'] = s9;cards[0]['showtimes'].append(s)
        return r

    def test_an_alert_that_sent_does_not_repeat_while_another_waits(self):
        ingest(self.db, self.job, self.page('0', '0'));self.db.commit()
        # B is inside its 30-minute gap, so it waits; A sends.
        self.db.execute('UPDATE alerts SET last_alert=? WHERE id=?', (time.time() - 60, self.b));self.db.commit()
        ingest(self.db, self.job, self.page('3', '3'));self.db.commit()
        self.db.execute('UPDATE outbox SET delivered=1')
        self.db.execute('UPDATE alerts SET last_alert=? WHERE id=?', (time.time() - 700, self.a));self.db.commit()
        ingest(self.db, self.job, self.page('3', '3', '3'));self.db.commit()
        new = [r[0] for r in self.db.execute('SELECT text FROM outbox WHERE delivered=0 AND alert_id=?', (self.a,))]
        self.assertEqual(len(new), 1)
        self.assertIn('/TEST/9/', new[0])
        self.assertNotIn('/TEST/1/', new[0])


class AlertStateTests(unittest.TestCase):
    def setUp(self):
        from api_source import parse
        self.tmp = tempfile.TemporaryDirectory();self.db = connect(str(Path(self.tmp.name) / 's.db'))
        self.show = next(iter(parse(response(status='3'), EVENT, DAY)['shows'].values()));self.show['movie'] = 'Test Movie'
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)', (self.show['id'], json.dumps(self.show), self.show['cutoff']));self.db.commit()
        self.base = {**prefs.defaults(), 'movies': ['Test Movie'], 'enabled': True, 'once': True}
        self.id = prefs.save(self.db, self.base, create=True);self.db.commit()

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def status(self):
        return next(r for r in prefs.rules(self.db) if r['id'] == self.id)['status']

    def test_photo_follow_up_does_not_complete_the_alert(self):
        import telegram
        from unittest.mock import patch
        self.db.execute("INSERT INTO outbox(id,text,photo,created,alert_id) VALUES('p',?,NULL,?,?)", (prefs.PHOTO_MARK + ' 3 seats open', time.time(), self.id))
        self.db.commit()
        with patch('telegram.send'):
            self.assertIsNone(telegram.drain(self.db))

    def test_paused_alert_is_not_completed_by_a_late_delivery(self):
        prefs.save(self.db, {**self.base, 'enabled': False}, self.id);self.db.commit()
        prefs.delivered(self.db, self.id)
        self.assertEqual(self.db.execute('SELECT completed FROM alerts WHERE id=?', (self.id,)).fetchone()[0], 0)

    def test_already_open_seats_wait_for_a_pending_message_instead_of_being_skipped(self):
        self.db.execute("INSERT INTO outbox(id,text,photo,created,alert_id) VALUES('old','x',NULL,?,?)", (time.time(), self.id));self.db.commit()
        prefs.prime_availability(self.db)
        self.assertIsNone(self.db.execute('SELECT 1 FROM alert_initial_state WHERE alert_id=?', (self.id,)).fetchone())
        self.db.execute("UPDATE outbox SET delivered=1");self.db.commit()
        prefs.prime_availability(self.db)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM outbox WHERE text LIKE '%already available%'").fetchone())

    def test_editing_an_alert_drops_gates_for_showtimes_it_no_longer_matches(self):
        prefs.queue_seat_photo(self.db, self.id, self.show['id'], ['Gold'], 'gate', 'text', time.time());self.db.commit()
        prefs.save(self.db, {**self.base, 'cinemas': ['ELSEWHERE']}, self.id)
        self.assertIsNone(self.db.execute('SELECT 1 FROM pending_seat_photos WHERE alert_id=?', (self.id,)).fetchone())

    def test_no_recovery_message_when_the_alert_is_paused(self):
        import health_watch
        from store import setmeta
        key = 'source:' + prefs.normalized('Test Movie')
        prefs.save(self.db, {**self.base, 'enabled': False}, self.id)
        setmeta(self.db, 'health_watch_issues', json.dumps({key: 'No successful check for Test Movie'}));self.db.commit()
        health_watch.check(self.db)
        self.assertIsNone(self.db.execute("SELECT 1 FROM outbox WHERE text LIKE '%recovered%'").fetchone())
