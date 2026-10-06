import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import low_float as lf


def candidate(symbol="ANPA", **updates):
    return {"symbol": symbol, "free_float": 2_000_000, "market_cap": 20_000_000,
            "active": True, "type": "CS", "locale": "us", "primary_exchange": "XNAS",
            "float_checked_at": lf.now(), "details_checked_at": lf.now(),
            "daily_analysis": {"checked_at": lf.now(), "ready": False},
            "price": {"price": 2, "received_at": lf.now()}, **updates}


def chart(floor_index=17, today_low=None):
    day = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)
    days = []
    while len(days) < 20:
        if day.weekday() < 5: days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    quote = {"high": [2] * 20, "low": [1.1] * 20, "close": [1.15] * 20}
    quote['low'][floor_index] = 1
    for i in range(floor_index, 20): quote['high'][i] = 1.3
    if today_low is not None:
        days.append(datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc))
        quote['high'].append(1.3); quote['low'].append(today_low); quote['close'].append(1.1)
    return {"meta": {"exchangeTimezoneName": "America/New_York"},
            "timestamp": [int(day.timestamp()) for day in days], "indicators": {"quote": [quote]}}


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

    def test_market_queue_prioritizes_new_price_and_retries_expensive_stocks(self):
        old = "2020-01-01T00:00:00+00:00"
        lf.ROWS.update(OLD=candidate("OLD", price={"price": 2, "received_at": old}),
                       NEW=candidate("NEW", free_float=4000000, price=None),
                       EXPENSIVE=candidate("EXPENSIVE", price={"price": 8, "received_at": old}),
                       FRESH=candidate("FRESH", rsi_updated_at=lf.now()),
                       RETRY=candidate("RETRY", price=None, market_attempted_at=lf.now()))
        queue = lf.market_queue({})
        self.assertEqual(queue[0], 'NEW')
        self.assertIn('EXPENSIVE', queue)
        self.assertNotIn('FRESH', queue)
        self.assertNotIn('RETRY', queue)

    def test_market_batch_is_bounded_and_reads_new_stock_before_old_backlog(self):
        for i in range(15):
            sym = 'OLD'+str(i)
            lf.ROWS[sym] = candidate(sym, price={"price":2,"received_at":"2020-01-01T00:00:00+00:00"}, rsi_updated_at=lf.now())
        lf.ROWS['NEW'] = candidate('NEW', free_float=4390000, price=None, rsi_updated_at=lf.now())
        active, maximum, calls = 0, 0, []
        async def fetch(client, sem, sym):
            nonlocal active, maximum
            active += 1; maximum = max(maximum, active); calls.append(sym)
            await asyncio.sleep(0.001)
            active -= 1
            return sym, {"price":4.99,"received_at":lf.now()}
        count = asyncio.run(lf.refresh_market_batch(None, {}, fetch))
        self.assertEqual(count, 12)
        self.assertEqual(maximum, 2)
        self.assertEqual(calls[0], 'NEW')
        self.assertIn('NEW', lf.snapshot({})['rows'])
        self.assertEqual(len(calls), 12)
        self.assertEqual(lf.STATUS['market_worker_version'], 2)

    def test_pagination_rejects_external_host_before_authorization(self):
        async def run():
            with self.assertRaises(ValueError):
                await lf.massive_get(None, 'https://example.com/page', 'secret')
        asyncio.run(run())

    def test_completed_sessions_and_intraday_break_even_after_price_recovery(self):
        at = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
        analysis = lf.daily_analysis('TEST', chart(today_low=.95), at)
        self.assertEqual(analysis['last_completed_session'], '2026-10-05')
        self.assertEqual(analysis['stability_sessions'], 2)
        self.assertEqual(analysis['support'], 1)
        row = candidate(daily_analysis=analysis, rsi_daily=27, rsi_updated_at=lf.now(), price={'price':1.15})
        result = lf.formation(row, at)
        self.assertEqual(result['state'], 'broken')
        self.assertEqual(result['stability_sessions'], 0)
        self.assertFalse(result['candidate'])
        # Live quote lows also catch a break before the next history refresh.
        analysis['live_low'] = None
        row['price'].update(day_low=.99, market_timestamp=at.isoformat())
        self.assertEqual(lf.formation(row, at)['state'], 'broken')

    def test_twenty_session_drawdown_distance_and_new_low_reset(self):
        at = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
        analysis = lf.daily_analysis('TEST', chart(), at)
        row = candidate(daily_analysis=analysis, rsi_daily=29.99, rsi_updated_at=lf.now(), price={'price':1.2})
        result = lf.formation(row, at)
        self.assertEqual(result['drawdown_pct'], 40)
        self.assertEqual(result['distance_pct'], 20)
        self.assertEqual(result['state'], 'steady')
        self.assertTrue(result['candidate'])
        row['price']['price'] = 1.201
        self.assertFalse(lf.formation(row, at)['candidate'])
        row['price']['price'] = 1.1
        for update in ({'rsi_daily':30}, {'market_cap':100000000}, {'rsi_daily':None},
                       {'rsi_updated_at':'2020-01-01T00:00:00+00:00'}):
            self.assertFalse(lf.formation({**row, **update}, at)['candidate'])
        row['daily_analysis'] = lf.daily_analysis('TEST', chart(floor_index=19), at)
        self.assertEqual(lf.formation(row, at)['state'], 'watch')
        self.assertEqual(lf.formation(row, at)['stability_sessions'], 0)

    def test_retest_requires_prior_departure_and_completed_stability(self):
        at = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
        result = chart(floor_index=16)
        result['indicators']['quote'][0]['low'][-1] = 1.02
        analysis = lf.daily_analysis('TEST', result, at)
        self.assertEqual(analysis['retest_date'], '2026-10-05')
        result['indicators']['quote'][0]['high'][17:19] = [1.04,1.04]
        result['indicators']['quote'][0]['low'][17:19] = [1.01,1.01]
        result['indicators']['quote'][0]['close'][17:19] = [1.03,1.03]
        self.assertIsNone(lf.daily_analysis('TEST', result, at)['retest_date'])
        # An equal low is a retest, not a new lower low resetting stability.
        result = chart(floor_index=16)
        result['indicators']['quote'][0]['low'][-1] = 1
        self.assertEqual(lf.daily_analysis('TEST', result, at)['stability_sessions'], 3)

    def test_short_gappy_stale_and_early_close_history(self):
        at = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
        result = chart()
        result['timestamp'] = result['timestamp'][1:]
        self.assertFalse(lf.daily_analysis('TEST', result, at)['ready'])
        result = chart()
        result['indicators']['quote'][0]['close'][-2] = None
        self.assertFalse(lf.daily_analysis('TEST', result, at)['ready'])
        result = chart(today_low=1.1)
        result['meta']['currentTradingPeriod']={'regular':{'end':int(datetime(2026,10,6,17,tzinfo=timezone.utc).timestamp())}}
        analysis = lf.daily_analysis('TEST', result, at)
        self.assertEqual(analysis['last_completed_session'], '2026-10-06')
        self.assertEqual(analysis['stability_sessions'], 3)
        self.assertFalse(lf.daily_analysis('TEST', chart(), at + timedelta(days=10))['ready'])
        analysis['checked_at'] = '2020-01-01T00:00:00+00:00'
        self.assertFalse(lf.formation(candidate(daily_analysis=analysis), at)['ready'])

    def test_history_upgrade_reuses_rsi_request_and_borrow_delta_is_observed(self):
        row = candidate(rsi_updated_at=lf.now())
        row['daily_analysis'] = None
        lf.ROWS['ANPA'] = row
        calls = []
        def handler(request):
            calls.append(request.url)
            return httpx.Response(200, json={'chart':{'result':[chart()]}}, request=request)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async def unused(*args): raise AssertionError('fresh price must be reused')
                await lf.refresh_market_batch(client, {}, unused)
        original_analysis = lf.daily_analysis
        with patch.object(lf, 'daily_analysis', side_effect=lambda sym, result: original_analysis(sym, result, datetime(2026, 10, 6, 18, tzinfo=timezone.utc))):
            asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertTrue(row['daily_analysis']['ready'])
        lf.update_borrow({'ANPA':{'available':12000}}, lf.now())
        self.assertNotIn('borrow_change', row)
        lf.update_borrow({'ANPA':{'available':8000}}, lf.now())
        self.assertEqual(row['borrow_change']['delta'], -4000)
        previous = copy.deepcopy(row['borrow_change'])
        lf.update_borrow({'ANPA':{'available':8000}}, lf.now())
        self.assertEqual(row['borrow_change'], previous)


if __name__ == '__main__':
    unittest.main()
