import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import watch_journal as w


def row(**values):return {'TEST':{'values':values,'context':'2026-10-01','market':'split'}}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.dbpatch=patch.object(w.storage,'DB_PATH',Path(self.temp.name)/'watch.sqlite3');self.dbpatch.start()
        self.j=w.Journal();self.j.restore()
    def tearDown(self):self.dbpatch.stop();self.temp.cleanup()
    def test_first_observation_is_not_a_change_and_zero_is_valid(self):
        self.j.ingest(row(available=0,stability=2,retest='waiting'),1000)
        self.assertEqual(self.j.page()['events'],[])
        self.assertEqual(self.j.current['TEST']['values']['available'],0)
    def test_down_and_up_crossings_are_saved_once(self):
        self.j.ingest(row(available=25000),1000)
        self.j.ingest(row(available=5000),1020)
        self.j.ingest(row(available=5000),1040)
        self.j.ingest(row(available=20000),1060)
        events=self.j.page()['events'];self.assertEqual(len(events),2)
        self.assertEqual(events[0]['before'],{'available':25000})
        self.assertEqual(events[1]['after'],{'available':20000})
        self.assertEqual(events[0]['kinds'],['available'])
    def test_null_stale_and_nan_never_become_false_crossings(self):
        self.j.ingest(row(available=5000),1000)
        self.j.ingest(row(available=None,stability=float('nan')),1020)
        self.assertEqual(self.j.page()['events'],[])
        self.assertEqual(self.j.current['TEST']['values'],{})
        self.j.ingest(row(available=12000),1040)
        self.assertEqual(self.j.page()['events'][0]['before']['available'],5000)
    def test_restart_restores_baseline_without_duplicate_events(self):
        self.j.ingest(row(available=20000),1000);self.j.ingest(row(available=1000),1020)
        reboot=w.Journal();reboot.restore();reboot.ingest(row(available=1000),1040)
        self.assertEqual(len(reboot.page()['events']),1)
    def test_arbitrary_custom_threshold_can_replay_small_raw_changes(self):
        self.j.ingest(row(available=14000),1000);self.j.ingest(row(available=13000),1020)
        event=self.j.page()['events'][0]
        self.assertEqual(event['kinds'],[])
        self.assertEqual(event['after']['available'],13000)
    def test_context_change_seeds_instead_of_comparing_split_units(self):
        self.j.ingest(row(support=.2,stability=4),1000)
        changed=row(support=2,stability=0);changed['TEST']['context']='2026-10-26'
        self.j.ingest(changed,1020)
        self.assertEqual(self.j.page()['events'],[])
    def test_ordered_pagination_and_requested_current_observations(self):
        self.j.ingest(row(available=20000),1000)
        for n in range(5):self.j.ingest(row(available=10000+n),1020+n*20)
        page=self.j.page(0,2,['TEST']);self.assertTrue(page['has_more']);self.assertEqual(len(page['events']),2)
        next_page=self.j.page(page['next_cursor'],2)
        self.assertGreater(next_page['events'][0]['seq'],page['events'][-1]['seq'])
        self.assertIn('TEST',page['observations']);self.assertEqual(next_page['observations'],{})
    def test_retention_is_explicit_and_does_not_silently_skip_gap(self):
        self.j.ingest(row(available=20000),1000)
        with patch.object(w,'MAX_ROWS',2):
            for n in range(4):self.j.ingest(row(available=10000+n),1020+n*20)
        page=self.j.page(0)
        self.assertEqual(len(page['events']),2);self.assertTrue(page['truncated'])
    def test_database_restore_resets_old_cursor_by_identity(self):
        self.j.ingest(row(available=20000),1000);self.j.ingest(row(available=5000),1020)
        first=self.j.page();page=self.j.page(100,journal_id='previous-database')
        self.assertTrue(page['reset']);self.assertEqual(len(page['events']),1)
        self.assertEqual(first['journal_id'],page['journal_id'])

if __name__=='__main__':unittest.main()
