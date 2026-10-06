import asyncio
import copy
import unittest
from unittest.mock import patch

import httpx
import low_float as lf


def candidate(symbol="ANPA", **updates):
    return {"symbol": symbol, "free_float": 2_000_000, "market_cap": 20_000_000,
            "active": True, "type": "CS", "locale": "us", "primary_exchange": "XNAS",
            "float_checked_at": lf.now(), "details_checked_at": lf.now(),
            "price": {"price": 2, "received_at": lf.now()}, **updates}


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.saved = copy.deepcopy((lf.ROWS, lf.CATALOG, lf.STATUS))
        lf.ROWS.clear(); lf.CATALOG.clear()

    def tearDown(self):
        for target, saved in zip((lf.ROWS, lf.CATALOG, lf.STATUS), self.saved):
            target.clear(); target.update(saved)

    def test_float_boundaries_and_rsi_not_required(self):
        self.assertTrue(lf.eligible(candidate(), {}))
        self.assertTrue(lf.eligible(candidate(rsi_daily=80), {}))
        self.assertTrue(lf.eligible(candidate(free_float=5_000_000), {}))
        for value in (None, 0, -1, 5_000_001, float('nan')):
            self.assertFalse(lf.eligible(candidate(free_float=value), {}))

    def test_reject_split_etf_unknown_cap_inactive_and_non_us(self):
        self.assertFalse(lf.eligible(candidate(), {"ANPA": {}}))
        for update in ({"type":"ETF"}, {"market_cap":None}, {"market_cap":300_000_000},
                       {"active":False}, {"locale":"global"}, {"primary_exchange":"OTC"}):
            self.assertFalse(lf.eligible(candidate(**update), {}))

    def test_snapshot_excludes_stale_fundamentals_and_new_splits(self):
        lf.ROWS.update(A=candidate("A"), B=candidate("B", details_checked_at="2020-01-01T00:00:00+00:00"))
        self.assertEqual(set(lf.snapshot({})['rows']), {'A'})
        self.assertEqual(lf.snapshot({'A':{}})['rows'], {})

    def test_expanding_ceiling_invalidates_old_catalog_without_erasing_rows(self):
        cached = {"low_float_rows": {"A": candidate("A")},
                  "low_float_catalog": {"A": {"symbol": "A", "free_float": 1000000}},
                  "low_float_status": {"last_complete_scan": lf.now(), "catalog_float_max": 2000000}}
        with patch.object(lf.storage, "load", return_value=cached):
            lf.restore()
        self.assertIsNone(lf.STATUS['last_complete_scan'])
        self.assertIn('A', lf.snapshot({})['rows'])
        cached['low_float_status']['catalog_float_max'] = lf.MAX_FLOAT
        with patch.object(lf.storage, "load", return_value=cached):
            lf.restore()
        self.assertEqual(lf.STATUS['last_complete_scan'], cached['low_float_status']['last_complete_scan'])

    def test_price_gate_is_strict_and_cannot_block_price_discovery(self):
        for value in (4.9999, 0.01):
            row = candidate(price={"price": value, "received_at": lf.now()})
            self.assertTrue(lf.price_allowed(row))
        for value in (5, 5.01, 10, 0, -1, None, float("nan"), float("inf")):
            row = candidate(price={"price": value, "received_at": lf.now()})
            self.assertTrue(lf.eligible(row, {}), "must still be polled for a later price change")
            self.assertFalse(lf.price_allowed(row))
            lf.ROWS['ANPA'] = row
            self.assertEqual(lf.snapshot({})['rows'], {})
        self.assertFalse(lf.price_allowed(candidate(price=None)))
        self.assertFalse(lf.price_allowed(candidate(price={"price": 2, "received_at": "2020-01-01T00:00:00+00:00"})))
        lf.ROWS['ANPA'] = candidate(price={"price": 4.99, "received_at": lf.now()})
        self.assertIn('ANPA', lf.snapshot({})['rows'])
        lf.ROWS['ANPA']['price']['price'] = 5
        self.assertNotIn('ANPA', lf.snapshot({})['rows'])

    def test_borrow_does_not_modify_split_rows_or_inputs(self):
        lf.ROWS['ANPA'] = candidate()
        parsed = {'ANPA': {'available':0, 'ctb':100}, 'DKI': {'available':10}}
        original = copy.deepcopy(parsed)
        lf.update_borrow(parsed, lf.now())
        self.assertEqual(parsed, original)
        self.assertEqual(lf.ROWS['ANPA']['borrow']['available'], 0)
        self.assertNotIn('DKI', lf.ROWS)

    def test_pagination_replaces_only_after_complete_scan(self):
        async def run(fail=False):
            lf.CATALOG['OLD'] = {'symbol':'OLD'}
            async def get(client, url, token, params=None):
                if url.endswith('page=2'):
                    if fail:
                        return httpx.Response(429, request=httpx.Request('GET', url))
                    data={'results':[{'ticker':'B','free_float':500000},{'ticker':'MID','free_float':4390000},{'ticker':'BIG','free_float':5000001}]}
                else:
                    data={'results':[{'ticker':'A','free_float':2000000}], 'next_url':'https://api.massive.com/stocks/vX/float?page=2'}
                return httpx.Response(200,json=data,request=httpx.Request('GET',url))
            with patch.object(lf,'massive_get',get), patch.object(lf,'save'):
                if fail:
                    with self.assertRaises(httpx.HTTPStatusError):
                        await lf.discover(None,'test')
                    self.assertEqual(set(lf.CATALOG), {'OLD'})
                else:
                    await lf.discover(None,'test')
                    self.assertEqual(set(lf.CATALOG), {'A','B','MID'})
                    self.assertEqual(lf.STATUS['scanned'],4)
        asyncio.run(run(True)); asyncio.run(run(False))

    def test_pagination_rejects_external_host_before_authorization(self):
        async def run():
            with self.assertRaises(ValueError):
                await lf.massive_get(None, 'https://example.com/page', 'secret')
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
