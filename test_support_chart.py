import copy
import unittest
from datetime import datetime, timezone

import support_chart as chart


class SupportChartTests(unittest.TestCase):
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
        wick = bar(2, '2026-10-02', 1.3, 1.45)
        self.assertEqual(chart.describe([formed, wick], 1.4, '2026-10-01')['retest']['status'], 'wick_reclaim')
        broken = bar(3, '2026-10-02', 1.2, 1.3)
        self.assertEqual(chart.describe([formed, wick, broken], 1.4, '2026-10-01')['retest']['status'], 'close_breach')
        live = bar(4, '2026-10-03', 1.1, 1.2, False)
        self.assertEqual(chart.describe([formed, wick, live], 1.4, '2026-10-01')['retest']['status'], 'wick_reclaim')

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
