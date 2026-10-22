import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from dashboard import handler
from local_credentials import load
from telegram import DeliveryError


class LocalSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.secret=Path(self.tmp.name)/'private'/'telegram.json'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(Path(self.tmp.name)/'state.sqlite3')))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        with urllib.request.urlopen(self.url+'/setup') as response:html=response.read().decode()
        self.csrf=re.search('name="csrf" value="([^"]+)"',html)[1]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def submit(self,origin=None,csrf=None):
        data=urllib.parse.urlencode({'csrf':csrf or self.csrf,'token':'12345:'+('a'*25),'chat':'12345'}).encode()
        req=urllib.request.Request(self.url+'/setup',data=data,headers={'Origin':origin or self.url})
        return urllib.request.urlopen(req)
    def test_cross_origin_and_invalid_csrf_never_send(self):
        with patch('telegram.send') as send:
            for kwargs in ({'origin':'http://evil.example'},{'csrf':'invalid'}):
                with self.assertRaises(urllib.error.HTTPError) as caught:self.submit(**kwargs)
                caught.exception.close()
            send.assert_not_called()
    def test_success_stores_owner_only_and_enables_loading(self):
        with patch('local_credentials.location',return_value=self.secret),patch('telegram.request'),patch('telegram.send') as send:
            with self.submit() as response:self.assertNotIn(b'12345:',response.read())
            send.assert_called_once()
            if os.name!='nt':self.assertEqual(self.secret.stat().st_mode & 0o777,0o600)
            with patch.dict(os.environ,{},clear=True):
                self.assertTrue(load());self.assertEqual(os.environ['TELEGRAM_CHAT_ID'],'12345')
    def test_failed_delivery_does_not_save_credentials(self):
        with patch('local_credentials.location',return_value=self.secret),patch('telegram.request'),patch('telegram.send',side_effect=DeliveryError()):
            with self.assertRaises(urllib.error.HTTPError) as caught:self.submit()
            caught.exception.close()
            self.assertFalse(self.secret.exists())
    def test_malformed_local_file_does_not_crash_monitor(self):
        self.secret.parent.mkdir();self.secret.write_text('invalid')
        with patch('local_credentials.location',return_value=self.secret),patch.dict(os.environ,{},clear=True):
            self.assertFalse(load())
