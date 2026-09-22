import unittest
from unittest.mock import patch
import main

class LatestSplitTests(unittest.TestCase):
    def setUp(self):
        self.old_universe=main.UNIVERSE.copy()
        self.old_history=main.HISTORY.copy()
        self.old_analytics=main.ANALYTICS.copy()
        self.old_trail=main.TRAIL.copy()

    def tearDown(self):
        main.UNIVERSE.clear();main.UNIVERSE.update(self.old_universe)
        main.HISTORY.clear();main.HISTORY.update(self.old_history)
        main.ANALYTICS.clear();main.ANALYTICS.update(self.old_analytics)
        main.TRAIL.clear();main.TRAIL.update(self.old_trail)

    def test_whlr_supersedes_august_split_and_invalidates_old_metrics(self):
        main.UNIVERSE["WHLR"]={"symbol":"WHLR","effective_date":"2026-08-27","source":"stockanalysis"}
        main.HISTORY["WHLR"]={"effective_date":"2026-08-27","post_split_high":100}
        main.ANALYTICS["WHLR"]={"ready":True}
        main.TRAIL["WHLR"]={"old":True}
        changed=main.apply_confirmed_splits()
        self.assertIn("WHLR",changed)
        self.assertEqual(main.UNIVERSE["WHLR"]["effective_date"],"2026-09-22")
        self.assertEqual(main.UNIVERSE["WHLR"]["ratio"],"1 for 9")
        self.assertNotIn("WHLR",main.HISTORY)
        self.assertNotIn("WHLR",main.ANALYTICS)
        self.assertNotIn("WHLR",main.TRAIL)
        self.assertEqual(main.apply_confirmed_splits(),[])

    def test_older_confirmed_date_never_overwrites_newer_split(self):
        main.UNIVERSE["WHLR"]={"symbol":"WHLR","effective_date":"2026-10-01","source":"exchange"}
        main.apply_confirmed_splits()
        self.assertEqual(main.UNIVERSE["WHLR"]["effective_date"],"2026-10-01")

if __name__=="__main__":unittest.main()
