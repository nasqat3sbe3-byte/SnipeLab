import os
import sqlite3
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

    def test_operations_close_connections_and_failed_write_rolls_back(self):
        with tempfile.TemporaryDirectory() as d, patch.object(storage,"DB_PATH",storage.Path(d)/"snapshots.sqlite3"):
            opened=[]
            real_connect=storage.connect
            def tracked_connect():
                conn=real_connect();opened.append(conn);return conn
            with patch.object(storage,"connect",tracked_connect):
                storage.save({"quotes":{"price":2}})
                storage.load(("quotes",))
                storage.snapshot_info()
                with self.assertRaises(sqlite3.ProgrammingError):
                    storage.save({"quotes":{"price":99},object():{}})
                self.assertEqual(storage.load(("quotes",))["quotes"]["price"],2)
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):conn.execute("SELECT 1")

    def test_market_saves_preserve_catalog_without_rewriting_it(self):
        import low_float
        with tempfile.TemporaryDirectory() as d, patch.object(storage,"DB_PATH",storage.Path(d)/"snapshots.sqlite3"), patch.object(low_float,"CATALOG",{"ANPA":{"free_float":123}}), patch.object(low_float,"ROWS",{"ANPA":{"price":2}}), patch.object(low_float,"STATUS",{"status":"ready"}):
            low_float.save()
            catalog_at=storage.snapshot_info()["collections"]["low_float_catalog"]["saved_at_epoch"]
            low_float.ROWS["ANPA"]["price"]=3
            low_float.save(include_catalog=False)
            self.assertEqual(storage.load(("low_float_rows",))["low_float_rows"]["ANPA"]["price"],3)
            self.assertEqual(storage.load(("low_float_catalog",))["low_float_catalog"]["ANPA"]["free_float"],123)
            self.assertEqual(storage.snapshot_info()["collections"]["low_float_catalog"]["saved_at_epoch"],catalog_at)

if __name__=="__main__":unittest.main()
