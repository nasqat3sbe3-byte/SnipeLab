import unittest
from rsi import normalized_closes, wilder_rsi
from history import calculate


class RsiTests(unittest.TestCase):
    def test_wilder_reference_vector(self):
        # Wilder's published 14-period example: first RSI is 70.4641.
        closes=[44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,
                45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        self.assertAlmostEqual(wilder_rsi(closes),70.464135,places=5)

    def test_ucar_mixed_scale_and_idempotence(self):
        raw=[('2026-07-28',1.0),('2026-07-29',20.2),('2026-07-30',1.01),
             ('2026-09-02',.609),('2026-09-03',.484),('2026-09-04',.471),
             ('2026-09-10',7.4),('2026-09-11',4.97),('2026-09-14',3.98)]
        fixed,repairs=normalized_closes('UCAR',raw)
        self.assertTrue(repairs)
        self.assertAlmostEqual(dict(fixed)['2026-07-28'],20)
        self.assertAlmostEqual(dict(fixed)['2026-07-29'],20.2)
        self.assertAlmostEqual(dict(fixed)['2026-09-04'],9.42)
        again,repairs=normalized_closes('UCAR',fixed)
        self.assertEqual(again,fixed)
        self.assertEqual(repairs,[])
        self.assertEqual(dict(fixed)['2026-09-10'],7.4)

    def test_mgn_cancelled_action_and_idempotence(self):
        raw=[('2026-09-03',140.4),('2026-09-04',120),('2026-09-08',3),
             ('2026-09-09',8.28),('2026-09-17',3.72)]
        fixed,repairs=normalized_closes('MGN',raw)
        self.assertTrue(repairs)
        self.assertEqual(dict(fixed)['2026-09-04'],3)
        self.assertEqual(dict(fixed)['2026-09-09'],8.28)
        self.assertEqual(normalized_closes('MGN',fixed),(fixed,[]))

    def test_other_symbols_and_real_rallies_unchanged(self):
        raw=[('2026-09-04',120),('2026-09-08',3),('2026-09-09',8)]
        self.assertEqual(normalized_closes('OTHER',raw),(raw,[]))
        self.assertEqual(normalized_closes('MGN',[('2026-09-04',10),('2026-09-08',3)])[1],[])

    def test_history_price_extrema_not_modified_by_rsi_repair(self):
        bars=[{'date':day,'open':v,'high':v,'low':v,'close':v} for day,v in
              [('2026-09-04',120),('2026-09-08',3),('2026-09-17',3.72)]]
        result=calculate('2026-09-17',bars,symbol='MGN')
        self.assertEqual(result['post_split_low'],3.72)
        self.assertEqual(bars[0]['close'],120)
        self.assertTrue(result['rsi_source_repairs'])

if __name__=='__main__':unittest.main()
