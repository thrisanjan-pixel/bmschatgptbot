import base64
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from dashboard import handler

class HostedDashboardTests(unittest.TestCase):
    def test_private_routes_and_disabled_setup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DASHBOARD_PASSWORD':'test-password-at-least-16'}):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(Path(tmp)/'state.sqlite3'),public=True))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            auth='Basic '+base64.b64encode(b'screenwatch:test-password-at-least-16').decode()
            try:
                with urllib.request.urlopen(url+'/healthz') as r:self.assertEqual(r.status,200)
                for path in ('/','/api/status','/setup'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+path)
                    self.assertEqual(caught.exception.code,401);caught.exception.close()
                for path in ('/','/api/status'):
                    with urllib.request.urlopen(urllib.request.Request(url+path,headers={'Authorization':auth})) as r:self.assertEqual(r.status,200)
                for data in (None,b'token=not-a-real-secret'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url+'/setup',data=data,headers={'Authorization':auth}))
                    self.assertEqual(caught.exception.code,403);caught.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()
