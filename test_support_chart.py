import copy
import unittest
from datetime import datetime, timezone

import support_chart as chart


class SupportChartTests(unittest.TestCase):
    def test_eviction_preserves_retest_and_disk_chart_without_provider_call(self):
        import asyncio,tempfile,storage,time
        from unittest.mock import patch
        h={'A':{'verified':True,'effective_date':'2026-09-01','post_split_low':1,'post_split_low_date':'2026-10-01'}}
        def bar(t,low,close):
            return {'time':t,'date':'2026-10-01','local_time':f'2026-10-01 {t}:00','low':low,'high':1.2,'close':close,'closed':True,'samples':4,'extended':True}
        stored={'split_date':'2026-09-01','fetched_epoch':time.time(),'hourly_version':1,'updated_at':datetime.now(timezone.utc).isoformat(),'candles':[bar(1,1,1.02),bar(2,1.08,1.1),bar(3,1.04,1.06)]}
        with tempfile.TemporaryDirectory() as d,patch.object(storage,'DB_PATH',storage.Path(d)/'test.sqlite3'),patch.object(chart,'CACHE',{}),patch.object(chart,'META',{}),patch.object(chart,'OBSERVATIONS',{}),patch.object(chart,'_HOT',set()),patch.object(chart,'MAX_CACHED_CHARTS',1),patch.object(chart,'fetch',side_effect=AssertionError('Unexpected provider request')):
            storage.save({'support_chart:A':stored,'support_chart:B':stored})
            summaries,charts=chart.restore_summaries(['A','B'],h,{'A','B'})
            self.assertEqual(len(charts),1);self.assertEqual(len(summaries),2)
            chart.META.update(summaries)
            chart.retain('A',stored)
            before=chart.retest_signal('A',h)
            self.assertEqual(before['support_retest_status'],'success')
            chart.retain('B',stored)
            self.assertNotIn('A',chart.CACHE)
            self.assertEqual(chart.retest_signal('A',h),before)
            asyncio.run(chart.load_cached('A',h))
            self.assertEqual(chart.get('A',h)['candles'],stored['candles'])
            self.assertEqual(len(chart.CACHE),1)
            h['A']['post_split_low']=.95
            self.assertEqual(chart.retest_signal('A',h)['support_retest_status'],'waiting')

    def test_extended_low_is_in_four_hour_candle(self):
        times = [int(datetime(2026, 10, 2, h, tzinfo=chart.NY).timestamp()) for h in (4, 5, 8, 9, 16, 17)]
        result = {'timestamp': times, 'meta': {'exchangeTimezoneName': 'America/New_York'},
                  'indicators': {'quote': [{'open': [2]*6, 'high': [2.1]*6,
                                           'low': [1.4, 1.8, 1.9, 1.8, 1.7, 1.6], 'close': [2]*6}]}}
        bars = chart.aggregate_hourly(result, datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]['local_time'], '2026-10-02 04:00')
        self.assertEqual(bars[0]['low'], 1.4)
        self.assertTrue(bars[0]['extended'])
        self.assertEqual(bars[-1]['local_time'], '2026-10-02 16:00')

    def test_regular_half_hour_alignment_does_not_cross_four_hour_boundary(self):
        times = [int(datetime(2026, 10, 2, h, 30, tzinfo=chart.NY).timestamp()) for h in (9,10,11,12,13,14,15)]
        result = {'timestamp': times, 'meta': {}, 'indicators': {'quote': [{'open':[2]*7,'high':[3]*7,'low':[1]*7,'close':[2]*7}]}}
        bars = chart.aggregate_hourly(result, datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual([b['local_time'] for b in bars], ['2026-10-02 09:30','2026-10-02 13:30'])
        self.assertEqual(bars[0]['samples'],4)
        self.assertTrue(bars[1]['session_partial'])

    def test_wick_and_close_breaches_differ_and_live_is_excluded(self):
        def bar(t, day, low, close, closed=True):
            return {'time': t, 'date': day, 'local_time': day+' 16:00', 'low': low, 'high': 2,
                    'open': 1.6, 'close': close, 'closed': closed, 'samples': 4, 'extended': True}
        formed = bar(1, '2026-10-01', 1.4, 1.5)
        rise = bar(2, '2026-10-01', 1.5, 1.6)
        wick = bar(3, '2026-10-02', 1.3, 1.45)
        self.assertEqual(chart.describe([formed, rise, wick], 1.4, '2026-10-01')['retest']['status'], 'wick_reclaim')
        broken = bar(3, '2026-10-02', 1.2, 1.3)
        self.assertEqual(chart.describe([formed, rise, wick, broken], 1.4, '2026-10-01')['retest']['status'], 'close_breach')
        live = bar(4, '2026-10-03', 1.1, 1.2, False)
        self.assertEqual(chart.describe([formed, rise, wick, live], 1.4, '2026-10-01')['retest']['status'], 'wick_reclaim')

    def test_five_percent_retest_requires_prior_escape_and_closed_return(self):
        def bar(t, low, close, closed=True):
            return {'time': t, 'date': '2026-10-01', 'local_time': f'2026-10-01 {t:02}:00',
                    'low': low, 'high': max(close, low, 1.07), 'open': close,
                    'close': close, 'closed': closed, 'samples': 4, 'extended': True}
        formed = bar(1, 1, 1.01)
        near = bar(2, 1.03, 1.04)
        rise = bar(3, 1.02, 1.10)
        returned = bar(4, 1.05, 1.06)
        def retest(bars, support=1, date='2026-10-01'):
            return chart.describe(bars, support, date)['retest']
        self.assertEqual(retest([formed, near])['status'], 'not_tested')
        self.assertEqual(retest([formed, rise])['status'], 'not_tested')
        self.assertEqual(retest([formed, rise, bar(4,1.0501,1.06)])['status'], 'not_tested')
        self.assertEqual(retest([formed, rise, bar(4,1.04,1.06,False)])['status'], 'not_tested')
        success = retest([formed, rise, returned])
        self.assertEqual(success['status'], 'touch_held')
        self.assertEqual(success['time'], returned['local_time'])
        self.assertEqual(retest([formed,rise,returned,bar(5,.99,.995)])['status'], 'close_breach')
        self.assertEqual(retest([formed,rise,returned],.95,'2026-10-02')['status'], 'not_tested')

    def test_signal_uses_cache_only_and_resets_when_support_changes(self):
        sym = 'RETEST_FIXTURE'
        h = {sym: {'verified': True,'effective_date':'2026-09-01',
                   'post_split_low': 1,'post_split_low_date':'2026-10-01'}}
        def bar(t, low, close):
            return {'time':t,'date':'2026-10-01','local_time':f'2026-10-01 {t:02}:00',
                    'low':low,'high':1.2,'close':close,'closed':True,'samples':4,'extended':True}
        chart.CACHE[sym] = {'split_date':'2026-09-01','fetched_epoch':1,
                            'candles':[bar(1,1,1.02),bar(2,1.08,1.1),bar(3,1.04,1.06)]}
        original = copy.deepcopy(h)
        try:
            self.assertEqual(chart.retest_signal(sym,h)['support_retest_status'],'success')
            self.assertEqual(h,original)
            self.assertNotIn(sym,chart.PRIORITY)
            h[sym]['post_split_low'] = .95
            h[sym]['post_split_low_date'] = '2026-10-02'
            self.assertEqual(chart.retest_signal(sym,h)['support_retest_status'],'waiting')
            h[sym]['effective_date'] = '2026-10-03'
            self.assertEqual(chart.retest_signal(sym,h)['support_retest_status'],'waiting')
        finally:
            chart.CACHE.pop(sym,None)
            chart.OBSERVATIONS.pop(sym,None)

    def test_cached_response_never_changes_core_history(self):
        h = {'ABC': {'verified': True, 'effective_date': '2026-09-01', 'post_split_low': 1.4, 'post_split_low_date': '2026-10-01'}}
        original = copy.deepcopy(h)
        chart.CACHE['ABC'] = {'split_date': '2026-08-01', 'candles': [{'time': 1}], 'fetched_epoch': 0}
        self.assertEqual(chart.get('ABC', h)['candles'], [])
        self.assertEqual(h, original)
        chart.CACHE.pop('ABC', None)
        chart.PRIORITY.pop('ABC', None)

    def test_unknown_support_time_and_inadequate_age_not_invented(self):
        bars = [{'time': 1, 'date': '2026-10-02', 'local_time': '2026-10-02 04:00',
                 'low': 1.5, 'high': 2, 'close': 1.8, 'open': 1.7, 'closed': True, 'samples': 4, 'extended': True}]
        d = chart.describe(bars, 1.4, '2026-10-01', '2026-10-03')
        self.assertIsNone(d['formation_time'])
        self.assertIsNone(d['age_sessions'])


if __name__ == '__main__':
    unittest.main()
