import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from api_source import parse, seat_layout_url
from store import connect, setmeta
from tests.test_api_watch import DAY, EVENT, response
import row_agent_protocol as agent
import row_watch
import watch_preferences as prefs


class RowAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(str(Path(self.tmp.name)/'state.sqlite3'))
        self.show=next(iter(parse(response(),EVENT,DAY)['shows'].values()))
        self.show['movie']='Movie'
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,1)',(self.show['id'],json.dumps(self.show),self.show['cutoff']))
        self.db.commit()
        row_watch.request(self.db,self.show['id'])
        self.data={'rows':[{'id':'Gold / A','category':'Gold','row':'A'}],
                   'available':[],'url':seat_layout_url(self.show)}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_signed_requests_reject_replay_and_changes(self):
        stamp=str(int(time.time()))
        nonce='abcdefghijklmnop123456'
        headers={'X-Row-Time':stamp,'X-Row-Nonce':nonce,
                 'X-Row-Signature':agent.signature('a-long-private-password','GET','/api/row-agent/jobs',stamp,nonce)}
        seen={}
        self.assertTrue(agent.verify('a-long-private-password','GET','/api/row-agent/jobs',headers,b'',seen))
        self.assertFalse(agent.verify('a-long-private-password','GET','/api/row-agent/jobs',headers,b'',seen))
        self.assertFalse(agent.verify('a-long-private-password','POST','/api/row-agent/jobs',headers,b'',{}))

    def test_successful_checks_are_due_after_30_seconds(self):
        now=time.time()
        self.assertEqual(len(agent.jobs(self.db,now)),1)
        self.assertTrue(agent.connected(self.db,now))
        agent.observation(self.db,{'show_id':self.show['id'],'data':copy.deepcopy(self.data)},now)
        self.assertEqual(agent.jobs(self.db,now+29),[])
        self.assertEqual(len(agent.jobs(self.db,now+31)),0)  # No active alert yet.
        prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'row_show':self.show['id'],
                            'rows':['Gold / A'],'enabled':True})
        self.assertEqual(len(agent.jobs(self.db,now+31)),1)

    def test_row_release_is_silent_on_baseline_and_not_repeated(self):
        agent.jobs(self.db)
        agent.observation(self.db,{'show_id':self.show['id'],'data':copy.deepcopy(self.data)})
        prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'row_show':self.show['id'],
                            'rows':['Gold / A'],'enabled':True})
        changed=copy.deepcopy(self.data)
        changed['available']=[{'row_id':'Gold / A','seat':'4'}]
        agent.observation(self.db,{'show_id':self.show['id'],'data':changed})
        agent.observation(self.db,{'show_id':self.show['id'],'data':changed})
        alerts=[r[0] for r in self.db.execute('SELECT text FROM outbox')]
        self.assertEqual(len(alerts),1)
        self.assertIn(seat_layout_url(self.show),alerts[0])

    def test_failed_read_backs_off_and_never_invents_rows(self):
        now=time.time()
        agent.observation(self.db,{'show_id':self.show['id'],'error':'Seat page unavailable (HTTP 403)'},now)
        self.assertEqual(agent.jobs(self.db,now+30),[])
        self.assertEqual(row_watch.get(self.db,self.show['id'])['data'],{})


if __name__=='__main__':
    unittest.main()
