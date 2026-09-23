import os
import tempfile
import unittest
from unittest.mock import patch
import storage

class StorageTests(unittest.TestCase):
    def test_snapshot_survives_reconnect(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(storage,"DB_PATH",storage.Path(d)/"snapshots.sqlite3"):
                storage.save({"universe":{"RETO":{"effective_date":"2026-05-18"}},"quotes":{"RETO":{"price":2.5}},"history":{"RETO":{"verified":True}}})
                restored=storage.load(("universe","quotes","history"))
                self.assertEqual(restored["quotes"]["RETO"]["price"],2.5)
                self.assertEqual(restored["history"]["RETO"]["verified"],True)
                storage.save({"quotes":{"RETO":{"price":3.0}}})
                self.assertEqual(storage.load(("quotes",))["quotes"]["RETO"]["price"],3.0)
                self.assertIn("RETO",storage.load(("universe",))["universe"])
                meta=storage.snapshot_info()
                self.assertEqual(meta["count"],3)
                self.assertIn("saved_at_epoch",meta["collections"]["quotes"])

if __name__=="__main__":unittest.main()
