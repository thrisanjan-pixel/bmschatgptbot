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
