import json
import time
import unittest
from unittest.mock import patch

import http_source
import rate_test
import row_watch
import telegram
from store import connect, emit, maintenance_active, meta


class MaintenanceRateTests(unittest.TestCase):
    def test_429_sends_one_notice_and_restores_slower_pace(self):
        with connect(':memory:') as db:
            now = time.time()
            rate_test.start(db, 'fixture', now)
            rate_test.start(db, 'fixture', now + 120)
            self.assertEqual(float(meta(db, 'rate_test_start')), now)
            self.assertEqual([rate_test.test_gap(db, now + i*3600) for i in range(4)], [60, 30, 20, 10])
            self.assertFalse(maintenance_active(db, now + 86400))
            row_watch.schema(db)
            db.execute('INSERT INTO sessions VALUES(?,?,?,1)', ('show', json.dumps({'id': 'show'}), now+20000))
            emit(db, 'ordinary', 'Ordinary alert', alert_id='row')
            self.assertIsNone(db.execute('SELECT 1 FROM outbox').fetchone())
            db.commit()
            test_at = now + 3*3600

            class Limited(Exception):
                status = 429
                retry = 1200

            def read(_):
                key = http_source.reserve(db, 'https://services-in.bookmyshow.com/doTrans.aspx', test_at)
                http_source.finish(db, key, 429, {'Retry-After': '1200', 'RateLimit-Limit': '120', 'Set-Cookie': 'private'})
                raise Limited('Seat source HTTP 429')

            with patch('cloud_row_reader.read', side_effect=read), patch('time.time', return_value=test_at):
                rate_test.step(db, test_at)
                notice = db.execute('SELECT alert_id,text FROM outbox').fetchone()
                self.assertEqual(notice['alert_id'], '__rate_test__')
                self.assertIn('Retry-After: 1200', notice['text'])
                self.assertEqual(rate_test.test_gap(db, test_at), 20)
                report = rate_test.report(db, test_at)
                self.assertIn('120', report['evidence'][0]['rate_headers'])
                self.assertNotIn('private', report['evidence'][0]['rate_headers'])
                self.assertIn('Screenwatch maintenance', rate_test.page(report))
            with patch('cloud_row_reader.read') as mock_read:
                rate_test.step(db, test_at + 1199)
                mock_read.assert_not_called()
            with patch('time.time', return_value=test_at+1), patch('telegram.send') as send:
                self.assertEqual(telegram.drain(db, system_only=True), '__rate_test__')
                send.assert_called_once()
