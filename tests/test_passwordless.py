import json,os,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from dashboard import handler
class PasswordlessTests(unittest.TestCase):
 def test_public_dashboard_and_api_without_credentials_keep_csrf_and_setup_protection(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'SCREENWATCH_PASSWORDLESS':'1','DASHBOARD_PASSWORD':''}):
   server=ThreadingHTTPServer(('127.0.0.1',0),handler(str(Path(tmp)/'test.sqlite3'),public=True))
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url=f'http://127.0.0.1:{server.server_port}'
   try:
    with urllib.request.urlopen(url+'/') as r:self.assertIn(b'Schedule alert',r.read())
    with urllib.request.urlopen(url+'/login') as r:self.assertEqual(r.url,url+'/')
    with urllib.request.urlopen(url+'/api/watch?new=1') as r:
     data=json.load(r);self.assertTrue(data['csrf']);self.assertNotIn('DASHBOARD_PASSWORD',json.dumps(data))
    for path,data in [('/setup',None),('/api/watch',b'{}')]:
     with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(url+path,data=data)
     self.assertEqual(caught.exception.code,403);caught.exception.close()
   finally:server.shutdown();server.server_close();thread.join()
