"""Behavioral checks for notification modes and visible alert health."""
import copy
import unittest
from tests.test_rows import RowTests
import row_watch
import watch_preferences as prefs

class ImprovementTests(RowTests):
 def test_future_only_rows_ignore_open_baseline_but_notify_later_release(self):
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'categories':['Gold'],'row_show':self.show['id'],'rows':['Gold / B'],'enabled':True,'notify_existing':False})
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
  changed=copy.deepcopy(self.result);changed['available'].append({'row_id':'Gold / B','seat':'2'})
  row_watch.ingest(self.db,self.show,changed)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
 def test_now_mode_notifies_available_selected_row(self):
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'row_show':self.show['id'],'rows':['Gold / B'],'enabled':True,'notify_existing':True})
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
 def test_failed_read_health_retains_last_success(self):
  self.configure()
  self.db.execute("UPDATE row_layouts SET error='Seat source HTTP 403'");self.db.commit()
  health=prefs.rules(self.db)[0]['health']
  self.assertEqual(health['label'],'BMS blocked one check')
  self.assertGreater(health['last_success'],0)
 def test_future_only_category_skips_initial_available_notification(self):
  row_watch.ingest(self.db,self.show,copy.deepcopy(self.result))
  prefs.save(self.db,{**prefs.defaults(),'movies':['Movie'],'enabled':True,'notify_existing':False})
  prefs.prime_availability(self.db)
  self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
 def test_mode_must_be_boolean(self):
  with self.assertRaises(ValueError):prefs.validate({**prefs.defaults(),'notify_existing':'false'})
