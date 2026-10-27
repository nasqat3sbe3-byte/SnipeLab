import asyncio
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
import httpx
import main
import watch_journal as w

class WatchRoutesTests(unittest.TestCase):
    def test_change_routes_and_asset_packaging(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(w.storage,'DB_PATH',Path(tmp)/'watch.sqlite3'):
            journal=w.Journal();journal.restore()
            journal.ingest({'TEST':{'values':{'available':20000},'context':'2026-10-01','market':'split'}})
            journal.ingest({'TEST':{'values':{'available':5000},'context':'2026-10-01','market':'split'}})
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    r=await client.get('/api/changes?after=0&symbols=TEST');self.assertEqual(r.status_code,200)
                    data=r.json();self.assertEqual(data['events'][0]['after']['available'],5000)
                    self.assertIn('TEST',data['observations']);self.assertTrue(data['journal_id'])
                    r=await client.get('/api/changes?after=-1');self.assertEqual(r.status_code,400)
                    for path in ['/assets/watch-plans.js','/assets/watch-plans.css','/dashboard','/low-float']:
                        response=await client.get(path);self.assertEqual(response.status_code,200,path)
                        self.assertIn('watch',response.text)
            with patch.object(w,'JOURNAL',journal):asyncio.run(run())
    def test_stale_or_missing_metrics_are_not_false_conditions(self):
        fresh=datetime.now(timezone.utc).isoformat();stale=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        with patch.object(main,'UNIVERSE',{'TEST':{'effective_date':'2026-10-01'}}),patch.object(main,'BORROW',{'TEST':{'available':0,'received_at':fresh}}),patch.object(main,'HISTORY',{}),patch.object(main,'ANALYTICS',{}),patch.object(main.low_float,'ROWS',{}):
            values=main._watch_rows()['TEST']['values'];self.assertEqual(values,{'available':0})
            main.BORROW['TEST']['received_at']=stale
            self.assertEqual(main._watch_rows()['TEST']['values'],{})

if __name__=='__main__':unittest.main()
