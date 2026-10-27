import base64
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

from api_source import parse,seat_layout_url
from cloud_row_reader import LAYOUT_KEY,parse_layout
from store import connect
from tests.test_api_watch import DAY,EVENT,response
import row_watch
import watch_preferences as prefs


def encrypted(text):
    pad=padding.PKCS7(128).padder()
    body=pad.update(text.encode())+pad.finalize()
    cipher=Cipher(algorithms.AES(LAYOUT_KEY),modes.CBC(bytes(16))).encryptor()
    return base64.b64encode(cipher.update(body)+cipher.finalize()).decode()


class CloudRowTests(unittest.TestCase):
    def setUp(self):
        self.show=next(iter(parse(response(),EVENT,DAY)['shows'].values()))
        self.show['movie']='Movie'
        self.areas={'0000000002':'Gold'}

    def test_real_area_mapping_and_live_status(self):
        text='GOLD:A:0000000002:1:N:0||1:R:A101+31:A201+30:A401+29:A000+00|1:S:A201+28:A101+27'
        data=parse_layout(self.show,encrypted(text),self.areas)
        self.assertEqual(data['url'],seat_layout_url(self.show))
        self.assertEqual({r['id'] for r in data['rows']},{'Gold / R','Gold / S'})
        self.assertEqual({(s['row_id'],s['seat']) for s in data['available']},
            {('Gold / R','31'),('Gold / R','29'),('Gold / S','27')})

    def test_corrupt_or_unknown_layout_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'decoded'):
            parse_layout(self.show,'garbage',self.areas)
        with self.assertRaisesRegex(ValueError,'Unknown seat status'):
            parse_layout(self.show,encrypted('GOLD:A:0000000002:1:N:0||1:R:A501+31'),self.areas)
        with self.assertRaisesRegex(ValueError,'No matching seat categories'):
            parse_layout(self.show,encrypted('GOLD:A:0000000002:1:N:0||1:R:A101+31'),{})

    def test_active_row_rule_is_due_at_30_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            db=connect(str(Path(folder)/'state.sqlite3'))
            try:
                db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(self.show['id'],json.dumps(self.show),self.show['cutoff']))
                db.commit()
                row_watch.request(db,self.show['id'])
                data={'rows':[{'id':'Gold / R','category':'Gold','row':'R'}],
                      'available':[],'url':seat_layout_url(self.show)}
                row_watch.ingest(db,self.show,copy.deepcopy(data))
                prefs.save(db,{**prefs.defaults(),'movies':['Movie'],'row_show':self.show['id'],
                    'rows':['Gold / R'],'enabled':True})
                now=time.time()
                row_watch.schedule_active(db,now+29,30)
                self.assertEqual(row_watch.get(db,self.show['id'])['due'],0)
                row_watch.schedule_active(db,now+31,30)
                self.assertGreater(row_watch.get(db,self.show['id'])['due'],0)
            finally:db.close()


if __name__=='__main__':unittest.main()
