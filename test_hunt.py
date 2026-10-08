import unittest
from datetime import datetime,timezone
import hunt
class HuntTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,8,19,tzinfo=timezone.utc)
        self.meta={'symbol':'TEST','effective_date':'2026-09-01'}
        self.h={'verified':True,'effective_date':'2026-09-01','split_day_4h_high':6,'updated_at':self.at.isoformat(),'hunt_daily_bars':[{'date':d,'low':2,'high':2.4} for d in hunt.window(self.at)]}
        self.q={'price':2.1,'received_at':self.at.isoformat(),'market_timestamp':self.at.isoformat()}
    def result(self):return hunt.evaluate(self.meta,self.h,self.q,{}, {},self.at)
    def test_price_boundaries(self):
        for price,state in [(2.4,'eligible'),(1.95,'eligible'),(2.401,'outside'),(1.949,'outside')]:
            self.q['price']=price;self.assertEqual(self.result()[0],state)
    def test_fixed_split_high_ignores_old_rally(self):
        self.h.update(post_split_high=100,half_reference_high=100,top_10_gain_pct=1000)
        self.h['hunt_daily_bars'].insert(0,{'date':'2026-09-01','low':1,'high':10})
        state,row=self.result();self.assertEqual(state,'eligible');self.assertEqual(row['half'],3)
    def test_seventy_excludes_even_after_pullback(self):
        self.h['hunt_daily_bars'][-2]['high']=3.4
        self.assertEqual(self.result()[0],'excluded')
    def test_reverse_order_does_not_count_as_rise(self):
        self.h['hunt_daily_bars'][0].update(low=4,high=4.1)
        self.assertEqual(self.result()[0],'eligible')
    def test_missing_session_holds(self):
        self.h['hunt_daily_bars'].pop(3);self.assertEqual(self.result()[0],'pending')
    def test_same_session_uncertainty_holds(self):
        self.h['hunt_daily_bars'][0].update(low=2,high=3.4);self.assertEqual(self.result()[0],'pending')
    def test_partial_split_candle_holds(self):
        self.h['partial_exchange_coverage']=True;self.assertEqual(self.result()[0],'pending')
    def test_today_high_blocks(self):
        self.q.update(day_low=2,day_high=3.5);self.assertEqual(self.result()[0],'excluded')
    def test_ten_sessions_ignore_old_peak(self):
        self.h['hunt_daily_bars'].insert(0,{'date':'2026-09-23','low':1,'high':8});self.assertEqual(self.result()[0],'eligible')
if __name__=='__main__':unittest.main()
