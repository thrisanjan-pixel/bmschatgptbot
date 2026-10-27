"""Multiple saved alerts must survive migration and notify independently."""
import json
import tempfile
import unittest
from pathlib import Path

from api_source import show_url
from api_watch import ingest
from store import connect, setmeta
import watch_preferences as prefs
from tests.test_api_watch import DAY, EVENT, response


class AlertRuleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.folder.name) / 'state.sqlite3'))
        self.job = {'id': 'discovery:' + show_url(EVENT, DAY),
                    'payload': json.dumps({'event': EVENT, 'date': DAY,
                                           'movie': 'Test Movie', 'notify_first': False})}

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_existing_running_alert_migrates_without_resetting_it(self):
        old = {**prefs.defaults(), 'movies': ['Test Movie'], 'enabled': True}
        setmeta(self.db, 'watch_preferences', json.dumps(old))
        self.db.commit()
        saved = prefs.rules(self.db)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['id'], 'legacy')
        self.assertTrue(saved[0]['preferences']['enabled'])
        self.assertEqual(saved[0]['preferences']['movies'], ['Test Movie'])

    def test_two_alerts_emit_separately_and_pause_is_scoped(self):
        first = prefs.save(self.db, {**prefs.defaults(), 'movies': ['Test Movie'],
            'new_shows': True, 'enabled': True}, create=True)
        second = prefs.save(self.db, {**prefs.defaults(), 'movies': ['Test Movie'],
            'new_shows': True, 'enabled': True}, create=True)
        ingest(self.db, self.job, response())
        ingest(self.db, self.job, response('2'))
        self.db.commit()
        pending = [r['alert_id'] for r in self.db.execute('SELECT alert_id FROM outbox WHERE delivered=0')]
        self.assertEqual(set(pending), {first, second})
        prefs.save(self.db, {**prefs.read(self.db, first), 'enabled': False}, first)
        pending = [r['alert_id'] for r in self.db.execute('SELECT alert_id FROM outbox WHERE delivered=0')]
        self.assertEqual(pending, [second])
        self.assertFalse(prefs.read(self.db, first)['enabled'])
        self.assertTrue(prefs.read(self.db, second)['enabled'])

    def test_once_completion_only_pauses_its_own_alert(self):
        first = prefs.save(self.db, {**prefs.defaults(), 'movies': ['Test Movie'],
            'enabled': True, 'once': True}, create=True)
        second = prefs.save(self.db, {**prefs.defaults(), 'movies': ['Test Movie'],
            'enabled': True, 'once': False}, create=True)
        prefs.delivered(self.db, first)
        self.db.commit()
        self.assertFalse(prefs.read(self.db, first)['enabled'])
        self.assertTrue(prefs.read(self.db, second)['enabled'])
        self.assertGreater(next(r for r in prefs.rules(self.db) if r['id'] == first)['completed'], 0)

    def test_existing_open_category_notifies_new_rule_once_with_exact_link(self):
        ingest(self.db,self.job,response(status='3'))
        self.db.commit()
        setting={**prefs.defaults(),'movies':['Test Movie'],'cinemas':['TEST'],
            'dates':[DAY],'categories':['Gold'],'new_shows':False,
            'availability':True,'enabled':True}
        alert_id=prefs.save(self.db,setting,create=True)
        prefs.prime_availability(self.db)
        prefs.prime_availability(self.db)
        messages=list(self.db.execute('SELECT text FROM outbox WHERE alert_id=?',(alert_id,)))
        self.assertEqual(len(messages),1)
        self.assertIn('Seats already available',messages[0][0])
        self.assertIn(f'/seat-layout/{EVENT}/TEST/1/{DAY}',messages[0][0])

    def test_closed_category_waits_for_open_transition(self):
        ingest(self.db,self.job,response(status='0'))
        self.db.commit()
        alert_id=prefs.save(self.db,{**prefs.defaults(),'movies':['Test Movie'],
            'new_shows':False,'availability':True,'enabled':True},create=True)
        prefs.prime_availability(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        ingest(self.db,self.job,response(status='3'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox WHERE alert_id=?',(alert_id,)).fetchone()[0],1)


if __name__ == '__main__':
    unittest.main()
