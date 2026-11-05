import io
import os
import unittest
from unittest.mock import patch
from telegram import request,DeliveryError

class TelegramErrorTests(unittest.TestCase):
    def test_malformed_success_responses_become_retryable_errors(self):
        for payload in (b'not json',b'[]',b'{"ok":true}',b'null'):
            with self.subTest(payload=payload),patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'test-only'}),patch('urllib.request.urlopen',return_value=io.BytesIO(payload)):
                with self.assertRaises(DeliveryError):request('getMe',{})
    def test_retry_delay_is_always_bounded_number(self):
        for value,expected in [('60',60),('invalid',30),(None,30),(-1,30),(999999999,86400)]:
            self.assertEqual(DeliveryError(retry=value).retry,expected)

    def test_photo_timeout_retries_photo_and_never_sends_text(self):
        import telegram
        calls=[]
        def fake(method,data,photo=None):calls.append(method);raise DeliveryError(0,reason='TimeoutError')
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'t','TELEGRAM_CHAT_ID':'1'}),patch('telegram.request',fake):
            with self.assertRaises(DeliveryError):telegram.send('hello',b'png')
        self.assertEqual(calls,['sendPhoto'])
        calls.clear()
        def rejected(method,data,photo=None):
            calls.append(method)
            if method=='sendPhoto':raise DeliveryError(400,reason='bad photo')
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'t','TELEGRAM_CHAT_ID':'1'}),patch('telegram.request',rejected):telegram.send('hello',b'png')
        self.assertEqual(calls,['sendPhoto','sendMessage'])
    def test_drain_sends_outside_transaction_and_marks_stale_alert(self):
        import tempfile,time,telegram
        from pathlib import Path
        from store import connect,emit
        with tempfile.TemporaryDirectory() as folder:
            db=connect(str(Path(folder)/'s.db'));emit(db,'old','Seats open',alert_id='a');emit(db,'new','Fresh',alert_id='b')
            db.execute("UPDATE outbox SET created=? WHERE alert_id='a'",(time.time()-3600,));db.execute("UPDATE outbox SET delivered=1 WHERE alert_id='b'");db.commit()
            seen=[]
            def fake(text,photo=None):seen.append((text,db.in_transaction))
            with patch('telegram.send',fake):self.assertEqual(telegram.drain(db),'a')
            self.assertFalse(seen[0][1]);self.assertIn('Delayed alert from',seen[0][0]);self.assertIn('IST (may be out of date)',seen[0][0]);self.assertTrue(seen[0][0].endswith('Seats open'))
            self.assertEqual(db.execute("SELECT delivered>0 FROM outbox WHERE alert_id='a'").fetchone()[0],1)
            emit(db,'fresh','Now',alert_id='c');db.commit()
            with patch('telegram.send',fake):telegram.drain(db)
            self.assertEqual(seen[1][0],'Now');db.close()
