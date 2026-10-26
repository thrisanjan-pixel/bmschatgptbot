import base64
import os
import re
import urllib.parse
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
                with urllib.request.urlopen(url+'/') as r:self.assertIn(b'Website password',r.read())
                for path in ('/api/status','/setup'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+path)
                    self.assertEqual(caught.exception.code,401);caught.exception.close()
                for path in ('/','/api/status'):
                    with urllib.request.urlopen(urllib.request.Request(url+path,headers={'Authorization':auth})) as r:self.assertEqual(r.status,200)
                for data in (None,b'token=not-a-real-secret'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url+'/setup',data=data,headers={'Authorization':auth}))
                    self.assertEqual(caught.exception.code,403);caught.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_form_login_requires_password_and_issues_protected_cookie(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'DASHBOARD_PASSWORD':'test-password-at-least-16'}):
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(Path(tmp)/'state.sqlite3'),public=True))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(url+'/') as response:page=response.read().decode()
                csrf=re.search(r'name="csrf" value="([^"]+)"',page)[1]
                for fields,code in [({'password':'test-password-at-least-16'},403),({'csrf':csrf,'password':'wrong'},401)]:
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+'/login',data=urllib.parse.urlencode(fields).encode())
                    self.assertEqual(caught.exception.code,code);caught.exception.close()
                opener=urllib.request.build_opener(NoRedirect)
                with self.assertRaises(urllib.error.HTTPError) as caught:opener.open(url+'/login',data=urllib.parse.urlencode({'csrf':csrf,'password':'test-password-at-least-16'}).encode())
                self.assertEqual(caught.exception.code,303)
                cookie=caught.exception.headers['Set-Cookie'];caught.exception.close()
                for protection in ('HttpOnly','Secure','SameSite=Strict'):self.assertIn(protection,cookie)
                with urllib.request.urlopen(urllib.request.Request(url+'/api/status',headers={'Cookie':cookie.split(';')[0]})) as response:self.assertEqual(response.status,200)
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(urllib.request.Request(url+'/api/status',headers={'Cookie':'screenwatch_session=forged'}))
                self.assertEqual(caught.exception.code,401);caught.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()
