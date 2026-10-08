import unittest
from datetime import datetime,timezone
import hunt
class HuntTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,8,19,tzinfo=timezone.utc)
        self.meta={'symbol':'TEST','effective_date':'2026-09-01'}
        self.h={'verified':True,'effective_date':'2026-09-01','split_day_4h_high':6,'updated_at':self.at.isoformat(),'hunt_daily_bars':[{'date':d,'low':2,'high':2.4} for d in hunt.window(self.at)]}
        self.borrow={'available':30000,'received_at':self.at.isoformat()}
        self.h['hunt_post_split_bars']=[{'date':d,'low':2,'high':2.4} for d in hunt.post_split_dates(self.meta['effective_date'],self.at)]
        self.q={'price':2.1,'received_at':self.at.isoformat(),'market_timestamp':self.at.isoformat()}
    def result(self):return hunt.evaluate(self.meta,self.h,self.q,self.borrow, {},self.at)
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
    def test_intraday_resolves_falling_session_range(self):
        days=hunt.window(self.at);candles=[]
        for i,d in enumerate(days):
            # The peak occurs before the new low; this is a decline, not a rally.
            levels=[(3.1,3.2),(3.1,3.2)] if i<4 else [(1.8,1.9),(1.75,1.85)]
            if i==4:levels=[(3.1,3.2),(1.75,1.85)]
            for n,(lo,hi) in enumerate(levels):
                candles.append({'date':d,'time':i*2+n,'local_time':d+(' 09:30' if n==0 else ' 13:30'),'low':lo,'high':hi,'closed':True,'samples':1})
        self.h.pop('hunt_daily_bars');detail={}
        chart={'updated_at':self.at.isoformat(),'candles':candles}
        state,row=hunt.evaluate(self.meta,self.h,self.q,self.borrow, {},self.at,chart,detail)
        self.assertEqual(state,'eligible');self.assertEqual(detail['movement_source'],'chronological_4h_extended')
        # A later upward move still triggers the same >=70% exclusion.
        candles[-1]['low']=2.9;candles[-1]['high']=3.0
        self.assertEqual(hunt.evaluate(self.meta,self.h,self.q,self.borrow, {},self.at,chart)[0],'excluded')
    def test_diagnostic_explains_missing_sessions(self):
        self.h['hunt_daily_bars'].pop(2);detail={}
        hunt.evaluate(self.meta,self.h,self.q,self.borrow, {},self.at,detail=detail)
        self.assertEqual(detail['state'],'pending');self.assertIn('10',detail['reason'])
    def test_available_boundary_and_missing(self):
        for available,state in [(0,'eligible'),(40000,'eligible'),(40001,'excluded'),(None,'pending')]:
            self.borrow['available']=available
            self.assertEqual(self.result()[0],state)
    def test_old_borrow_holds(self):
        self.borrow['received_at']='2026-09-01T00:00:00+00:00'
        self.assertEqual(self.result()[0],'pending')
    def test_two_waves_require_return_to_original_base(self):
        seq=[{'date':str(i),'low':lo,'high':hi} for i,(lo,hi) in enumerate([(1,1.1),(1.9,2),(2.4,2.5),(1.2,1.4),(2.2,2.4)])]
        self.assertEqual(len(hunt.waves80(seq)['events']),1)
        seq[3].update(low=1,high=1.1)
        self.assertEqual(len(hunt.waves80(seq)['events']),2)
    def test_eighty_is_strict_and_peak_extensions_count_once(self):
        self.assertEqual(len(hunt.waves80([{'date':'1','low':1,'high':1},{'date':'2','low':1.8,'high':1.8}])['events']),0)
        seq=[{'date':'1','low':1,'high':1},{'date':'2','low':1.9,'high':2},{'date':'3','low':2.3,'high':2.5}]
        self.assertEqual(len(hunt.waves80(seq)['events']),1)
    def test_hourly_parts_resolve_large_falling_four_hour_bar(self):
        days=hunt.window(self.at);candles=[]
        for i,d in enumerate(days):
            for slot in ['09:30','13:30']:
                low,high=(2,2.4)
                parts=[]
                if i==0 and slot=='09:30':
                    low,high=2,4
                    parts=[{'time':1,'date':d,'low':3.9,'high':4},{'time':2,'date':d,'low':2,'high':2.2}]
                candles.append({'time':i*100+(0 if slot=='09:30' else 30),'date':d,'local_time':d+' '+slot,'closed':True,'low':low,'high':high,'hourly_parts':parts})
        state,row=hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,{'updated_at':self.at.isoformat(),'candles':candles})
        self.assertEqual(state,'eligible')
    def test_two_rebounds_same_day_count_only_once(self):
        seq=[{'date':'2026-09-01','local_time':'2026-09-01 '+hour,'low':lo,'high':hi} for hour,lo,hi in [('09:30',1,1.1),('10:30',1.9,2),('11:30',1,1.1),('12:30',1.9,2)]]
        self.assertEqual(len(hunt.waves80(seq)['events']),1)
        seq.extend([{'date':'2026-09-02','local_time':'2026-09-02 09:30','low':1,'high':1.1},{'date':'2026-09-02','local_time':'2026-09-02 10:30','low':1.9,'high':2}])
        result=hunt.waves80(seq)
        self.assertEqual(len(result['events']),2)
        self.assertEqual([e['peak_session'] for e in result['events']],['2026-09-01','2026-09-02'])
if __name__=='__main__':unittest.main()
