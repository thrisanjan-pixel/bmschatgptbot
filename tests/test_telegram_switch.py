import tempfile,unittest,threading,json,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from store import connect,emit
from dashboard import handler
from telegram import drain
class TelegramSwitchTests(unittest.TestCase):
 def test_switch_csrf_persistence_and_delivery_gate(self):
  with tempfile.TemporaryDirectory() as folder:
   path=str(Path(folder)/'state.db');server=ThreadingHTTPServer(('127.0.0.1',0),handler(path));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
   try:
    with urllib.request.urlopen(base+'/api/watch?new=1') as r:token=json.load(r)['csrf']
    with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(base+'/api/telegram',data=b'{"enabled":false}')
    self.assertEqual(failure.exception.code,403);failure.exception.close()
    def toggle(enabled):
     with urllib.request.urlopen(urllib.request.Request(base+'/api/telegram',data=json.dumps({'enabled':enabled}).encode(),headers={'X-CSRF-Token':token,'Content-Type':'application/json'})) as r:return json.load(r)
    self.assertFalse(toggle(False)['enabled'])
    db=connect(path);emit(db,'ticket','ticket',alert_id='ticket');emit(db,'health','health',alert_id='__health__');db.commit();db.close();db=connect(path)
    try:
     with patch('telegram.send') as send:drain(db);drain(db,system_only=True);send.assert_not_called()
     self.assertTrue(toggle(True)['enabled'])
     with patch('telegram.send') as send:drain(db,system_only=True);send.assert_called_once()
     self.assertEqual(db.execute("SELECT delivered FROM outbox WHERE alert_id='ticket'").fetchone()[0],0)
    finally:db.close()
   finally:server.shutdown();server.server_close();thread.join()
