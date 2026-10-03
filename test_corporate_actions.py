import unittest
from corporate_actions import parse_notice, upcoming


class CorporateActionTests(unittest.TestCase):
    def test_execution_date_not_announcement_or_vote(self):
        a = parse_notice('Reverse Stock Split for ZCMD',
                         'Announced September 30, 2026. The vote is October 1, 2026. '
                         'The reverse stock split will become effective on Friday, October 2, 2026.',
                         'https://www.nasdaqtrader.com/TraderNews.aspx?id=test', 'ZCMD')
        self.assertEqual(a['effective_date'], '2026-10-02')
        self.assertEqual(upcoming({'a': a}, 'ZCMD', '2026-10-03'), [])
        self.assertEqual(upcoming({'a': a}, 'ZCMD', '2026-10-02'), [])
        self.assertEqual(len(upcoming({'a': a}, 'ZCMD', '2026-10-01')), 1)

    def test_consolidation_and_merger_are_distinct(self):
        self.assertEqual(parse_notice('Share consolidation', 'Shares will begin trading on October 6, 2026.', '', 'ABC')['kind'], 'reverse_split')
        self.assertEqual(parse_notice('Merger of ABC', 'The merger is expected to close on October 7, 2026.', '', 'ABC')['effective_date'], '2026-10-07')

    def test_missing_date_not_invented(self):
        a = parse_notice('Merger of ABC', 'Meeting on October 7, 2026. Closing date has not been announced.', '', 'ABC')
        self.assertIsNone(a['effective_date'])
        self.assertEqual(len(upcoming({'a': a}, 'ABC', '2026-10-03')), 1)

    def test_cancellation_completion_and_non_actions(self):
        for title in ['Merger of ABC (UPDATED: Merger closed)', 'Reverse Split cancelled']:
            self.assertIsNone(parse_notice(title, '', '', 'ABC'))
        self.assertIsNone(parse_notice('Quarterly earnings', '', '', 'ABC'))

    def test_unknown_old_split_expires_and_exchange_wins(self):
        a = {'symbol': 'ABC', 'kind': 'reverse_split', 'effective_date': None, 'published_date': '2026-01-01'}
        self.assertEqual(upcoming({'a': a}, 'ABC', '2026-10-03'), [])
        a.update(effective_date='2026-10-06', source='Nasdaq')
        b = dict(a, source='StockAnalysis')
        self.assertEqual(upcoming({'b': b, 'a': a}, 'ABC', '2026-10-03')[0]['source'], 'Nasdaq')


if __name__ == '__main__':
    unittest.main()
