"""BookMyShow source policy: sold-out replies, 403/429 handling, breaker, jitter, probe and home worker."""
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_source as source
import row_watch
import watch_preferences as prefs
from api_source import parse
from store import connect, meta, setmeta
from tests.test_api_watch import DAY, EVENT, response

SEATS = 'https://services-in.bookmyshow.com/doTrans.aspx'
HOST = 'services-in.bookmyshow.com'
SOLD_OUT_BODY = {'BookMyShow': {'blnSuccess': 'false', 'intException': '3306', 'intExceptionEx': '159',
    'strException': "It seems seats are sold out hence we can't proceed further. Please check another show. (#159)"}}


class Reply(io.BytesIO):
    status = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class SoldOutParserTests(unittest.TestCase):
    def show(self):
        return {'venue_code': 'AMBH', 'session': '1', 'event': EVENT, 'date': DAY, 'categories': {},
                'url': 'https://in.bookmyshow.com/x'}

    def read_with(self, body):
        import cloud_row_reader
        with patch('cloud_row_reader.open_request', return_value=Reply(json.dumps(body).encode())), \
             patch('cloud_row_reader.seat_layout_url', return_value='https://in.bookmyshow.com/x'):
            return cloud_row_reader.read(self.show())

    def test_3306_reply_is_sold_out_not_an_error(self):
        from cloud_row_reader import SoldOut
        with self.assertRaises(SoldOut) as caught:self.read_with(SOLD_OUT_BODY)
        self.assertTrue(caught.exception.sold_out)
        self.assertEqual(str(caught.exception), 'Sold out')

    def test_159_text_without_code_is_also_sold_out(self):
        from cloud_row_reader import refusal_kind
        self.assertEqual(refusal_kind({'strException': 'seats are sold out (#159)'}), 'sold_out')

    def test_other_refusals_stay_errors(self):
        from cloud_row_reader import SoldOut
        with self.assertRaises(ValueError) as caught:
            self.read_with({'BookMyShow': {'blnSuccess': 'false', 'intException': '1001', 'strException': 'Session expired'}})
        self.assertNotIsInstance(caught.exception, SoldOut)
        self.assertEqual(str(caught.exception), 'Seat source refused layout')


class HostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 's.db')
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_403_classification(self):
        self.assertEqual(source.classify(403, {'Server': 'cloudflare', 'CF-Ray': 'abc-BOM'}), 'cloudflare_block')
        self.assertEqual(source.classify(403, {'CF-Mitigated': 'challenge'}), 'cloudflare_block')
        self.assertEqual(source.classify(403, {'Server': 'nginx'}), 'forbidden')
        self.assertEqual(source.classify(429, {}), 'rate_limited')
        self.assertEqual(source.classify(0), 'network')
        self.assertEqual(source.classify(503), 'server_error')
        self.assertEqual(source.classify(200), 'ok')

    def test_403_backs_off_exponentially_and_success_resets(self):
        cf = {'Server': 'cloudflare', 'CF-Ray': 'x'}
        rests = []
        for _ in range(3):
            source.record_outcome(self.db, HOST, 403, cf, now=1000)
            rests.append(self.db.execute('SELECT not_before FROM source_hosts WHERE host=?', (HOST,)).fetchone()[0] - 1000)
        self.assertEqual(rests, [180, 360, 720])
        source.record_outcome(self.db, HOST, 200, {}, now=2000)
        self.assertEqual(self.db.execute('SELECT failures FROM source_health WHERE host=?', (HOST,)).fetchone()[0], 0)

    def test_block_alerts_the_operator_once_per_episode(self):
        cf = {'Server': 'cloudflare'}
        for _ in range(4):source.record_outcome(self.db, HOST, 403, cf, now=1000)
        texts = [r[0] for r in self.db.execute("SELECT text FROM outbox WHERE alert_id='__health__'")]
        self.assertEqual(len(texts), 1)
        self.assertIn('Cloudflare', texts[0])
        source.record_outcome(self.db, HOST, 200, {}, now=2000)
        for _ in range(2):source.record_outcome(self.db, HOST, 403, cf, now=3000)
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE alert_id='__health__'").fetchone()[0], 2)

    def test_429_cooldown_is_host_wide_and_survives_restart(self):
        now = time.time()
        source.record_outcome(self.db, HOST, 429, {'Retry-After': '5000'}, now=now);self.db.commit()
        self.db.close();self.db = connect(self.path)
        with self.assertRaises(source.SourceWait) as wait:source.reserve(self.db, SEATS, now + 100)
        self.assertGreaterEqual(wait.exception.until, now + 5000)
        # Manual and metadata lanes rest too: the cooldown belongs to the host.
        with self.assertRaises(source.SourceWait):source.reserve(self.db, SEATS, now + 100, manual=True)
        self.assertGreaterEqual(float(meta(self.db, 'seat_cooldown_until')), now + 5000)
        source.reserve(self.db, SEATS, now + 5001)

    def test_429_through_open_request_persists(self):
        error = urllib.error.HTTPError(SEATS, 429, 'Too Many', {'Retry-After': '4000', 'Server': 'cloudflare'}, io.BytesIO(b''))
        request = urllib.request.Request(SEATS, b'x')
        with patch.dict(os.environ, {'BMS_REQUEST_BUDGET': '1', 'BMS_DB': self.path}), \
             patch('http_source.urlopen', side_effect=error), self.assertRaises(urllib.error.HTTPError):
            source.open_request(request)
        self.db.close();self.db = connect(self.path)
        until = self.db.execute('SELECT not_before FROM source_hosts WHERE host=?', (HOST,)).fetchone()[0]
        self.assertGreater(until, time.time() + 3900)

    def test_circuit_breaker_opens_after_repeated_failures(self):
        source.record_outcome(self.db, HOST, 0, now=1000)
        source.record_outcome(self.db, HOST, 503, now=1000)
        self.assertIsNone(self.db.execute('SELECT not_before FROM source_hosts WHERE host=? AND not_before>0', (HOST,)).fetchone())
        source.record_outcome(self.db, HOST, 0, now=1000)
        self.assertEqual(self.db.execute('SELECT not_before FROM source_hosts WHERE host=?', (HOST,)).fetchone()[0], 1600)

    def test_jitter_spaces_alert_checks_between_60_and_75_seconds(self):
        with patch.dict(os.environ, {'BMS_REQUEST_JITTER': '15'}):
            source.reserve(self.db, SEATS, 1000)
            with self.assertRaises(source.SourceWait) as wait:source.reserve(self.db, SEATS, 1001)
            self.assertTrue(1060 <= wait.exception.until <= 1075)
            with self.assertRaises(source.SourceWait) as again:source.reserve(self.db, SEATS, 1002)
            self.assertEqual(wait.exception.until, again.exception.until)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.db = connect(str(Path(self.tmp.name) / 's.db'))
        # The fixture listing reports every category sold out (status '0').
        self.show = next(iter(parse(response(), EVENT, DAY)['shows'].values()));self.show['movie'] = 'Movie'
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)', (self.show['id'], json.dumps(self.show), self.show['cutoff']))
        self.result = {'rows': [{'id': 'Gold / A', 'category': 'Gold', 'row': 'A'}], 'available': [], 'seats': [], 'url': self.show['url']}
        # An alert can only pick rows from a layout that was read once.
        row_watch.schema(self.db)
        self.db.execute("INSERT INTO row_layouts(show_id,payload,checked) VALUES(?,?,?)", (self.show['id'], json.dumps(self.result), time.time() - 3600))
        prefs.save(self.db, {**prefs.defaults(), 'movies': ['Movie'], 'categories': ['Gold'], 'row_show': self.show['id'], 'rows': ['Gold / A'], 'enabled': True})
        self.db.commit()

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def step(self, read):
        setmeta(self.db, 'seat_next_request_at', 0);setmeta(self.db, 'seat_budget_until', 0);self.db.commit()
        with patch.dict(os.environ, {'BMS_PUBLIC': '1'}), patch('cloud_row_reader.read', side_effect=read) as reader:
            row_watch.step(self.db)
        return reader

    def row(self):
        return self.db.execute('SELECT * FROM row_layouts WHERE show_id=?', (self.show['id'],)).fetchone()

    def test_probe_before_full_layout(self):
        from cloud_row_reader import SoldOut
        row_watch.schema(self.db)
        self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?)", (self.show['id'], time.time() - 5))
        self.db.commit()
        # 1. The seat source says sold out: stored as a state, re-checked on the slow backstop.
        self.step(SoldOut())
        row = self.row();now = time.time()
        self.assertEqual(row['error'], 'Sold out')
        self.assertAlmostEqual(row['due'], now + 900, delta=5)
        self.assertGreater(row['sold_out_at'], 0)
        self.assertEqual(self.db.execute("SELECT failures FROM check_stats WHERE source='rows'").fetchone()[0], 0)
        # 2. Due again while the listing still says sold out: no seat request is spent.
        self.db.execute('UPDATE row_layouts SET due=? WHERE show_id=?', (time.time() - 1, self.show['id']));self.db.commit()
        reader = self.step(AssertionError('must not read'))
        reader.assert_not_called()
        self.assertGreater(self.row()['due'], time.time() + 800)
        # 3. The listing shows seats again: the full layout is read at once.
        opened = copy.deepcopy(self.show)
        for category in opened['categories'].values():category['status'] = '3'
        self.db.execute('UPDATE sessions SET payload=? WHERE id=?', (json.dumps(opened), self.show['id']));self.db.commit()
        reader = self.step(lambda *a, **k: copy.deepcopy(self.result))
        reader.assert_called_once()
        self.assertEqual((self.row()['error'], self.row()['sold_out_at']), ('', 0))

    def test_backstop_reads_even_if_listing_never_changes(self):
        from cloud_row_reader import SoldOut
        row_watch.schema(self.db)
        self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,due,sold_out_at,error) VALUES(?,'{}',?,?,'Sold out')",
            (self.show['id'], time.time() - 1, time.time() - 1000))
        self.db.commit()
        reader = self.step(SoldOut())
        reader.assert_called_once()

    def test_released_seats_after_sold_out_alert_again(self):
        row_watch.schema(self.db)
        self.db.execute("INSERT OR REPLACE INTO row_alert_state VALUES(?,?,?)",
            ('x', json.dumps([self.show['id'], ['Gold / A']]), json.dumps([['Gold / A', '1']])))
        self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload) VALUES(?,'{}')", (self.show['id'],))
        row_watch.mark_sold_out(self.db, self.show['id'], time.time(), True)
        self.assertEqual(self.db.execute("SELECT available FROM row_alert_state WHERE alert_id='x'").fetchone()[0], '[]')

    def test_shows_with_seats_are_never_gated_by_the_listing(self):
        row_watch.schema(self.db)
        self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?)", (self.show['id'], time.time() - 5))
        self.db.commit()
        reader = self.step(lambda *a, **k: copy.deepcopy(self.result))
        reader.assert_called_once()


class HomeWorkerTests(ProbeTests):
    TOKEN = 'w' * 40

    def env(self, **extra):
        return patch.dict(os.environ, {'BMS_SEAT_SOURCE': 'worker', 'BMS_ROWS_ENABLED': '1', 'BMS_WORKER_TOKEN': self.TOKEN, **extra})

    def due(self):
        row_watch.schema(self.db)
        self.db.execute("INSERT OR REPLACE INTO row_layouts(show_id,payload,due) VALUES(?,'{}',?)", (self.show['id'], time.time() - 5))
        self.db.commit()

    def test_claim_and_submit_layout(self):
        self.due()
        job = row_watch.claim(self.db)
        self.assertEqual(job['show_id'], self.show['id'])
        self.assertIsNone(row_watch.claim(self.db), 'a leased read is not handed out twice')
        row_watch.submit(self.db, job['show_id'], copy.deepcopy(self.result))
        self.assertEqual((self.row()['error'], self.row()['due']), ('', 0))
        with self.assertRaises(ValueError):row_watch.submit(self.db, job['show_id'], copy.deepcopy(self.result))

    def test_worker_sold_out_and_429_reports(self):
        self.due();job = row_watch.claim(self.db)
        row_watch.submit(self.db, job['show_id'], error={'message': 'Sold out', 'sold_out': True})
        self.assertEqual(self.row()['error'], 'Sold out')
        self.db.execute('UPDATE row_layouts SET due=?,sold_out_at=0 WHERE show_id=?', (time.time() - 1, self.show['id']));self.db.commit()
        job = row_watch.claim(self.db)
        row_watch.submit(self.db, job['show_id'], error={'message': 'Seat source HTTP 429', 'status': 429, 'retry': 5000})
        self.assertGreater(float(meta(self.db, 'seat_cooldown_until')), time.time() + 4900)

    def test_server_does_not_read_while_worker_mode_and_tells_operator_when_offline(self):
        self.due()
        with self.env():reader = self.step(AssertionError('server must not read'))
        reader.assert_not_called()
        self.assertEqual(meta(self.db, 'seat_worker_offline'), '1')
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE alert_id='__health__'").fetchone()[0], 1)
        with self.env():self.step(AssertionError('server must not read'))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE alert_id='__health__'").fetchone()[0], 1)

    def test_fallback_reads_on_the_server_when_worker_is_offline(self):
        self.due()
        with self.env(BMS_WORKER_FALLBACK='server'):reader = self.step(lambda *a, **k: copy.deepcopy(self.result))
        reader.assert_called_once()

    def test_worker_endpoint_requires_the_token(self):
        from dashboard import handler
        path = str(Path(self.tmp.name) / 's.db');self.due()
        with self.env():
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(path, public=True))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/api/worker/claim'
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(urllib.request.Request(url, b'{}', {'Authorization': 'Bearer wrong'}))
                self.assertEqual(denied.exception.code, 401)
                with urllib.request.urlopen(urllib.request.Request(url, b'{}', {'Authorization': 'Bearer ' + self.TOKEN})) as reply:
                    self.assertEqual(json.load(reply)['job']['show_id'], self.show['id'])
            finally:
                server.shutdown();server.server_close()


if __name__ == '__main__':
    unittest.main()


class SoldOutHealthTests(ProbeTests):
    def test_sold_out_alert_is_not_source_unavailable(self):
        self.db.execute("UPDATE row_layouts SET error='Sold out',checked=? WHERE show_id=?", (time.time(), self.show['id']));self.db.commit()
        health = next(r for r in prefs.rules(self.db) if r['status'] == 'active')['health']
        self.assertEqual(health['label'], 'Sold out · watching for released seats')
        self.assertEqual(health['error'], '')
