"""Hot-standby failover: role gate, /heartbeat /promote /demote, outbox merge, dedupe,
dead letters, Healthchecks pings, staleness and standby pacing."""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import failover
import role
import telegram
from store import connect, dedupe_tag, emit, meta, setmeta

SECRET = 's' * 40


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.sqlite3')
        self.db = connect(self.path)
        self.env = patch.dict(os.environ, {'HEARTBEAT_SECRET': SECRET}, clear=False)
        self.env.start()
        for name in ('BOT_ROLE', 'HEALTHCHECKS_URL', 'NTFY_TOPIC_URL', 'OUTBOX_AMBIGUOUS_POLICY', 'OUTBOX_MAX_ATTEMPTS',
                     'PROMOTE_FRESH_SECONDS', 'STANDBY_RATE_FACTOR', 'LITESTREAM_BUCKET'):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        self.db.close()
        self.tmp.cleanup()

    def standby(self):
        os.environ['BOT_ROLE'] = 'standby'
        role.initialize(self.db)

    def undelivered(self, db=None):
        return [r[0] for r in (db or self.db).execute('SELECT text FROM outbox WHERE delivered=0 ORDER BY created')]

    def replica(self, rows):
        """A primary database file holding the given outbox rows, as Litestream would restore it."""
        folder = tempfile.mkdtemp(dir=self.tmp.name)
        path = str(Path(folder) / 'restored.sqlite3')
        other = connect(path)
        for row in rows:
            other.execute('INSERT INTO outbox(id,text,photo,created,delivered,alert_id,dedupe,release_ts) VALUES(?,?,?,?,?,?,?,?)',
                          (row['id'], row['text'], None, row['created'], row.get('delivered', 0), row.get('alert_id', 'a1'),
                           row.get('dedupe', ''), row.get('release_ts', row['created'])))
        other.commit()
        other.close()
        return path


class RoleTests(Base):
    def test_primary_starts_active_standby_starts_standby(self):
        self.assertEqual(role.initialize(self.db), 'active')
        other = connect(str(Path(self.tmp.name) / 'b.sqlite3'))
        try:
            os.environ['BOT_ROLE'] = 'standby'
            self.assertEqual(role.initialize(other), 'standby')
        finally:
            other.close()

    def test_runtime_role_survives_restart(self):
        self.standby()
        failover.promote(self.path, restore=lambda prefix: None)
        self.db.close()
        self.db = connect(self.path)
        self.assertEqual(role.initialize(self.db), 'active')  # BOT_ROLE is still standby

    def test_role_switch_starts_and_stops_sender(self):
        self.standby()
        emit(self.db, 'x', 'Seats open', alert_id='a1')
        self.db.commit()
        with patch('telegram.send') as send:
            self.assertIsNone(telegram.drain(self.db))
        send.assert_not_called()
        self.assertEqual(self.undelivered(), ['Seats open'])
        failover.promote(self.path, restore=lambda prefix: None)
        with patch('telegram.send') as send:
            self.assertEqual(telegram.drain(self.db), 'a1')
        send.assert_called_once()
        failover.demote(self.path)
        emit(self.db, 'y', 'Later', alert_id='a1')
        self.db.commit()
        with patch('telegram.send') as send:
            self.assertIsNone(telegram.drain(self.db))
        send.assert_not_called()

    def test_reading_the_role_takes_no_write_lock(self):
        role.runtime_role(self.db)
        self.assertFalse(self.db.in_transaction)

    def test_standby_uses_reduced_bookmyshow_rate(self):
        from http_source import reserve, SourceWait
        self.standby()
        os.environ['STANDBY_RATE_FACTOR'] = '0.5'
        url = 'https://in.bookmyshow.com/api/x'
        reserve(self.db, url, now=1000)
        with self.assertRaises(SourceWait) as wait:
            reserve(self.db, url, now=1061)
        self.assertEqual(wait.exception.until, 1120)  # 60 s gap doubled
        failover.promote(self.path, restore=lambda prefix: None)
        self.db.close()
        self.db = connect(self.path)
        reserve(self.db, url, now=2000)
        with self.assertRaises(SourceWait) as wait:
            reserve(self.db, url, now=2030)
        self.assertEqual(wait.exception.until, 2060)


class EndpointTests(Base):
    def setUp(self):
        super().setUp()
        from dashboard import handler
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def call(self, route, body=None, secret=SECRET):
        headers = {'Content-Type': 'application/json'}
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        request = urllib.request.Request(self.base + route, data=json.dumps(body or {}).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_endpoints_require_the_heartbeat_secret(self):
        for route in ('/heartbeat', '/promote', '/demote'):
            for secret in (None, 'wrong' * 10):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.call(route, secret=secret)
                self.assertEqual(failure.exception.code, 401)
                failure.exception.close()

    def test_short_secret_disables_the_endpoints(self):
        os.environ['HEARTBEAT_SECRET'] = 'short'
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.call('/heartbeat', secret='short')
        self.assertEqual(failure.exception.code, 401)
        failure.exception.close()

    def test_heartbeat_reports_status_and_records_watchdog_view(self):
        setmeta(self.db, 'api_last_success', 1234.0)
        setmeta(self.db, 'last_telegram_success_at', 1200.0)
        emit(self.db, 'x', 'queued', alert_id='a1')
        self.db.commit()
        status, body = self.call('/heartbeat', {'instances': {'primary': {'last_seen': 5, 'role': 'active', 'ok': True}}})
        self.assertEqual(status, 200)
        self.assertEqual(body['role'], 'active')
        self.assertEqual(body['last_bms_success'], 1234.0)
        self.assertEqual(body['outbox_depth'], 1)
        self.assertEqual(body['last_telegram_success_at'], 1200.0)
        self.assertGreater(float(meta(self.db, 'watchdog_last_seen')), 0)
        self.assertEqual(json.loads(meta(self.db, 'watchdog_view'))['instances']['primary']['role'], 'active')

    def test_promote_is_idempotent(self):
        self.standby()
        with patch('failover.restore_replica', return_value=None) as restore:
            first = self.call('/promote')
            second = self.call('/promote')
        self.assertEqual(first, (200, {**first[1], 'role': 'active', 'changed': True}))
        self.assertEqual(second, (200, {'role': 'active', 'changed': False}))
        restore.assert_called_once()
        self.assertEqual(role.runtime_role(self.db), 'active')

    def test_demote_is_idempotent(self):
        self.assertEqual(self.call('/demote')[1], {'role': 'standby', 'changed': True})
        self.assertEqual(self.call('/demote')[1], {'role': 'standby', 'changed': False})

    def test_readyz_exposes_role_and_delivery_telemetry(self):
        self.standby()
        try:
            urllib.request.urlopen(self.base + '/readyz')
        except urllib.error.HTTPError as error:
            body = json.load(error)
            error.close()
        else:
            self.fail('a fresh database is not ready')
        self.assertEqual(body['role'], 'standby')
        for key in ('outbox_depth', 'last_telegram_success_at', 'last_failed_send_at', 'last_bms_success'):
            self.assertIn(key, body)

    def test_dashboard_status_has_failover_block(self):
        with urllib.request.urlopen(self.base + '/api/status') as response:
            data = json.load(response)
        for key in ('role', 'watchdog_last_seen', 'outbox_depth', 'last_telegram_success_at', 'litestream', 'healthchecks_last_ping'):
            self.assertIn(key, data['failover'])


class OutboxTests(Base):
    def test_outbox_survives_crash_mid_send(self):
        class Crash(BaseException):
            pass
        emit(self.db, 'x', 'Row F open', alert_id='a1')
        self.db.commit()
        with patch('telegram.send', side_effect=Crash):
            with self.assertRaises(Crash):
                telegram.drain(self.db)
        self.db.close()  # the process died; nothing was marked delivered
        self.db = connect(self.path)
        self.assertEqual(self.undelivered(), ['Row F open'])
        with patch('telegram.send') as send:
            self.assertIsNone(telegram.drain(self.db))  # still claimed for 120 s
            send.assert_not_called()
        self.db.execute('UPDATE outbox SET next_try=0')
        self.db.commit()
        with patch('telegram.send') as send:
            self.assertEqual(telegram.drain(self.db), 'a1')
        send.assert_called_once()
        self.assertEqual(self.undelivered(), [])

    def test_detection_writes_outbox_in_the_same_transaction(self):
        emit(self.db, 'x', 'Seats', alert_id='a1')
        self.assertTrue(self.db.in_transaction)  # the caller commits it with its state change
        self.db.rollback()
        self.assertEqual(self.undelivered(), [])

    def test_dedupe_key_prevents_double_alert_on_ambiguous_timeout(self):
        now = time.time()
        tag = dedupe_tag('SHOW1', 'F-7,8', now)
        for event in ('primary-copy', 'standby-copy'):
            emit(self.db, event, 'Row F open\n' + tag, alert_id='a1', dedupe=('SHOW1', 'F-7,8', now + (5 if event == 'standby-copy' else 0)))
        self.db.commit()
        calls = []

        def timeout(text, photo=None):
            calls.append(text)
            raise telegram.DeliveryError(reason='TimeoutError', ambiguous=True)
        with patch('telegram.send', timeout):
            telegram.drain(self.db)
        with patch('telegram.send') as send:
            telegram.drain(self.db)
            telegram.drain(self.db)
        send.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertIn('[SHOW1:F-7,8:', calls[0])
        self.assertEqual(self.undelivered(), [])
        self.assertEqual(meta(self.db, 'ambiguous_sends'), '1')
        self.assertEqual(meta(self.db, 'dedupe_skipped'), '1')
        self.assertGreater(float(meta(self.db, 'last_failed_send_at')), 0)

    def test_retry_policy_resends_after_ambiguous_timeout(self):
        os.environ['OUTBOX_AMBIGUOUS_POLICY'] = 'retry'
        emit(self.db, 'x', 'Seats', alert_id='a1')
        self.db.commit()
        with patch('telegram.send', side_effect=telegram.DeliveryError(reason='TimeoutError', ambiguous=True)):
            telegram.drain(self.db)
        self.assertEqual(self.undelivered(), ['Seats'])

    def test_read_timeout_is_ambiguous_but_connect_failure_is_not(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 't'}):
            with patch('urllib.request.urlopen', side_effect=TimeoutError()):
                with self.assertRaises(telegram.DeliveryError) as error:
                    telegram.request('sendMessage', {})
            self.assertTrue(error.exception.ambiguous)
            with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('refused')):
                with self.assertRaises(telegram.DeliveryError) as error:
                    telegram.request('sendMessage', {})
            self.assertFalse(error.exception.ambiguous)

    def test_dead_letter_after_bounded_retries_with_ntfy_meta_alert(self):
        os.environ['OUTBOX_MAX_ATTEMPTS'] = '3'
        emit(self.db, 'x', 'Seats', alert_id='a1')
        self.db.commit()
        with patch('telegram.send', side_effect=telegram.DeliveryError(502, reason='Bad Gateway')), patch('ntfy.notify') as notify:
            for _ in range(3):
                self.db.execute('UPDATE outbox SET next_try=0')
                self.db.commit()
                telegram.drain(self.db)
        notify.assert_called_once()
        self.assertEqual(self.undelivered(), [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM suppressed_outbox').fetchone()[0], 1)
        self.assertEqual(meta(self.db, 'dead_letter_count'), '1')

    def test_flood_waits_never_dead_letter(self):
        os.environ['OUTBOX_MAX_ATTEMPTS'] = '1'
        emit(self.db, 'x', 'Seats', alert_id='a1')
        self.db.commit()
        with patch('telegram.send', side_effect=telegram.DeliveryError(429, retry=30)), patch('ntfy.notify') as notify:
            telegram.drain(self.db)
        notify.assert_not_called()
        self.assertEqual(self.undelivered(), ['Seats'])

    def test_success_records_last_telegram_success(self):
        emit(self.db, 'x', 'Seats', alert_id='a1')
        self.db.commit()
        with patch('telegram.send'):
            telegram.drain(self.db)
        self.assertGreater(float(meta(self.db, 'last_telegram_success_at')), 0)


class PromotionTests(Base):
    def test_restore_then_send_drains_the_primarys_queued_alerts(self):
        self.standby()
        now = time.time()
        # The standby saw the F-row release too, while its sender was off.
        emit(self.db, 'standby-row', 'Row F open (standby copy)', alert_id='a1', dedupe=('SHOW1', 'F-7', now - 30))
        # And it has an old alert that queued hours ago: stale, must not flood Telegram.
        emit(self.db, 'standby-old', 'Old news', alert_id='a1')
        self.db.execute("UPDATE outbox SET created=? WHERE text='Old news'", (now - 7200,))
        self.db.commit()
        replica = self.replica([
            {'id': 'p-row', 'text': 'Row F open (primary copy)', 'created': now - 40, 'dedupe': 'SHOW1:F-7', 'release_ts': now - 40},
            {'id': 'p-new', 'text': 'New show 9 PM', 'created': now - 20, 'dedupe': 'SHOW2:h1', 'release_ts': now - 20},
            {'id': 'p-sent', 'text': 'Already sent', 'created': now - 50, 'delivered': now - 45},
            {'id': 'p-stale', 'text': 'Primary stale', 'created': now - 7200},
        ])
        result = failover.promote(self.path, restore=lambda prefix: replica)
        self.assertEqual(result['role'], 'active')
        self.assertEqual(result['merge'], {'added': 1, 'marked_delivered': 0, 'already_had': 1, 'stale_skipped': 1})
        self.assertEqual(result['stale_suppressed'], 1)
        sent = []
        with patch('telegram.send', lambda text, photo=None: sent.append(text)):
            for _ in range(5):
                telegram.drain(self.db)
        self.assertCountEqual(sent, ['Row F open (standby copy)', 'New show 9 PM'])
        self.assertEqual(self.undelivered(), [])
        self.assertFalse(Path(replica).exists())  # the temp restore is removed

    def test_rows_the_primary_delivered_are_not_sent_again(self):
        self.standby()
        now = time.time()
        emit(self.db, 'standby-row', 'Row F open', alert_id='a1', dedupe=('SHOW1', 'F-7', now - 30))
        self.db.commit()
        replica = self.replica([{'id': 'p-row', 'text': 'Row F open', 'created': now - 40, 'delivered': now - 35,
                                 'dedupe': 'SHOW1:F-7', 'release_ts': now - 40}])
        result = failover.promote(self.path, restore=lambda prefix: replica)
        self.assertEqual(result['merge']['marked_delivered'], 1)
        with patch('telegram.send') as send:
            telegram.drain(self.db)
        send.assert_not_called()

    def test_failed_restore_never_blocks_promotion(self):
        self.standby()

        def broken(prefix):
            raise RuntimeError('litestream restore failed: no route to R2')
        result = failover.promote(self.path, restore=broken)
        self.assertEqual(result['role'], 'active')
        self.assertIn('failed', result['merge'])
        self.assertEqual(role.runtime_role(self.db), 'active')

    def test_restore_replica_is_skipped_without_r2_settings(self):
        self.assertIsNone(failover.restore_replica())

    def test_restore_config_names_no_secret(self):
        with patch.dict(os.environ, {'LITESTREAM_SECRET_ACCESS_KEY': 'super-secret-value'}):
            folder = tempfile.mkdtemp(dir=self.tmp.name)
            config, _ = failover.litestream_config('primary', folder)
            text = Path(config).read_text()
        self.assertNotIn('super-secret-value', text)
        self.assertIn('path: primary', text)
        self.assertIn('${LITESTREAM_SECRET_ACCESS_KEY}', text)

    def test_standby_copies_alerts_from_the_primary(self):
        import watch_preferences as prefs
        self.standby()
        replica_path = self.replica([])
        primary = connect(replica_path)
        prefs.save(primary, {**prefs.defaults(), 'movies': ['Test Movie'], 'enabled': True}, create=True)
        setmeta(primary, 'notifications_paused', '0')
        primary.commit()
        primary.close()
        self.assertEqual(failover.maybe_sync_config(self.db, restore=lambda prefix: replica_path), 1)
        self.assertEqual([r['preferences']['movies'] for r in prefs.rules(self.db)], [['Test Movie']])
        # Not again within STANDBY_SYNC_SECONDS, and never while active.
        self.assertIsNone(failover.maybe_sync_config(self.db, restore=lambda prefix: self.fail('synced twice')))


class MonitoringTests(Base):
    def test_healthchecks_ping_at_most_once_a_minute(self):
        sent = []
        self.assertFalse(failover.ping_healthchecks(self.db, now=1000, send=lambda: sent.append(1)))  # not configured
        os.environ['HEALTHCHECKS_URL'] = 'https://hc-ping.com/test-uuid'
        self.assertTrue(failover.ping_healthchecks(self.db, now=1000, send=lambda: sent.append(1)))
        self.assertFalse(failover.ping_healthchecks(self.db, now=1059, send=lambda: sent.append(1)))
        self.assertTrue(failover.ping_healthchecks(self.db, now=1060, send=lambda: sent.append(1)))
        self.assertEqual(len(sent), 2)

    def test_listing_failure_never_pings_healthchecks(self):
        import api_watch
        os.environ['HEALTHCHECKS_URL'] = 'https://hc-ping.com/test-uuid'
        api_watch.seed(self.db)
        monitor = api_watch.Monitor(self.path)
        with patch('api_watch.get_json', side_effect=ConnectionResetError()), patch('failover.ping_healthchecks') as ping:
            monitor.check(self.db)
        ping.assert_not_called()

    def test_successful_listing_pings_healthchecks(self):
        import api_watch
        api_watch.seed(self.db)
        monitor = api_watch.Monitor(self.path)
        with patch('api_watch.get_json', return_value={'data': {}}), patch('api_watch.movies', return_value=[]), \
                patch('api_watch.next_job', side_effect=lambda db, now: db.execute("SELECT * FROM jobs WHERE kind='catalog'").fetchone()), \
                patch('failover.ping_healthchecks') as ping:
            monitor.check(self.db)
        ping.assert_called_once()

    def test_stale_payload_is_flagged_after_three_refresh_periods(self):
        with patch('ntfy.notify') as notify:
            self.assertFalse(failover.observe_payload(self.db, 'seats', 'S1', [['F', '7']], 600, now=0))
            self.assertFalse(failover.observe_payload(self.db, 'seats', 'S1', [['F', '7']], 600, now=1700))
            self.assertTrue(failover.observe_payload(self.db, 'seats', 'S1', [['F', '7']], 600, now=1800))
            self.assertTrue(failover.observe_payload(self.db, 'seats', 'S1', [['F', '7']], 600, now=2400))
            for thread in threading.enumerate():
                if thread is not threading.current_thread() and thread.daemon:
                    thread.join(timeout=1)
        self.assertEqual(notify.call_count, 1)
        self.assertIn('seats:S1', json.loads(meta(self.db, 'stale_sources')))
        self.assertFalse(failover.observe_payload(self.db, 'seats', 'S1', [['F', '8']], 600, now=2500))
        self.assertEqual(json.loads(meta(self.db, 'stale_sources')), {})

    def test_cloudflare_403_logs_the_ray_id(self):
        from http_source import record_outcome
        with self.assertLogs(level='WARNING') as logs:
            record_outcome(self.db, 'in.bookmyshow.com', 403, {'Server': 'cloudflare', 'CF-Ray': '8abc123-BOM'}, now=1000)
        self.assertTrue(any('8abc123-BOM' in line and 'cloudflare_block' in line for line in logs.output))

    def test_429_retry_after_persists_across_restarts(self):
        from http_source import record_outcome, reserve, SourceWait
        record_outcome(self.db, 'in.bookmyshow.com', 429, {'Retry-After': '3600'}, now=time.time())
        self.db.commit()
        self.db.close()
        self.db = connect(self.path)
        with self.assertRaises(SourceWait) as wait:
            reserve(self.db, 'https://in.bookmyshow.com/api/x')
        self.assertGreater(wait.exception.until, time.time() + 3500)

    def test_sold_out_3306_logs_at_info(self):
        import cloud_row_reader
        body = json.dumps({'BookMyShow': {'blnSuccess': 'false', 'intException': '3306', 'strException': 'sold out (#159)'}}).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *_):
                return body
        show = {'id': 'S1', 'event': 'ET1', 'venue_code': 'V', 'date': '20261125', 'session': '1', 'url': 'https://in.bookmyshow.com/x'}
        with patch('cloud_row_reader.open_request', return_value=Response()), patch('cloud_row_reader.seat_layout_url', return_value='https://in.bookmyshow.com/x'):
            with self.assertLogs(level='INFO') as logs, self.assertRaises(cloud_row_reader.SoldOut):
                cloud_row_reader.read(show)
        self.assertTrue(any(line.startswith('INFO') and '3306' in line for line in logs.output))


class WatchdogDecisionTests(unittest.TestCase):
    def test_watchdog_decision_function_node_tests_pass(self):
        """The Worker's pure decision function is tested with node --test (watchdog/test)."""
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import subprocess
        root = Path(__file__).resolve().parent.parent / 'watchdog'
        run = subprocess.run([node, '--test', str(root / 'test' / 'decide.test.js')], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
