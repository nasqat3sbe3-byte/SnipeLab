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
        self.chart={'updated_at':self.at.isoformat(),'split_date':self.meta['effective_date'],'candles':[]}
        for i,d in enumerate(hunt.post_split_dates(self.meta['effective_date'],self.at)):
            for n,slot in enumerate(['09:30','13:30']):
                self.chart['candles'].append({'date':d,'time':i*100+n*30,'local_time':d+' '+slot,'low':2,'high':2.4,'closed':True})
    def result(self):return hunt.evaluate(self.meta,self.h,self.q,self.borrow, {},self.at,self.chart)
    def test_price_boundaries(self):
        for price,state in [(2.55,'eligible'),(1.8,'eligible'),(2.551,'outside'),(1.799,'outside')]:
            self.q['price']=price;self.assertEqual(self.result()[0],state)
    def test_fixed_split_high_ignores_old_rally(self):
        self.h.update(post_split_high=100,half_reference_high=100,top_10_gain_pct=1000)
        self.h['hunt_daily_bars'].insert(0,{'date':'2026-09-01','low':1,'high':10})
        state,row=self.result();self.assertEqual(state,'eligible');self.assertEqual(row['half'],3)
    def test_seventy_excludes_even_after_pullback(self):
        self.chart=None
        self.h['hunt_daily_bars'][-2]['high']=3.4
        self.assertEqual(self.result()[0],'excluded')
    def test_daily_decline_alone_cannot_prove_extended_hours_clear(self):
        self.chart=None
        self.h['hunt_daily_bars'][0].update(low=4,high=4.1)
        self.assertEqual(self.result()[0],'pending')
    def test_missing_session_holds(self):
        self.chart=None
        self.h['hunt_daily_bars'].pop(3);self.assertEqual(self.result()[0],'pending')
    def test_same_session_uncertainty_holds(self):
        self.chart=None
        self.h['hunt_daily_bars'][0].update(low=2,high=3.4);self.assertEqual(self.result()[0],'pending')
    def test_partial_split_candle_holds(self):
        self.h['partial_exchange_coverage']=True;self.assertEqual(self.result()[0],'pending')
    def test_today_high_blocks(self):
        self.chart=None
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
        self.assertEqual(state,'pending');self.assertEqual(detail['movement_source'],'chronological_4h_extended');self.assertIn('بعد التقسيم',detail['reason'])
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
    def test_one_rally_is_enough_without_reset(self):
        seq=[{'date':str(i),'low':lo,'high':hi} for i,(lo,hi) in enumerate([(1,1.1),(1.9,2),(2.4,2.5),(1.2,1.4),(2.2,2.4)])]
        self.assertEqual(len(hunt.waves80(seq)['events']),1)
        self.assertEqual(hunt.waves80(seq)['events'][0]['gain_pct'],150)
    def test_eighty_inclusive_and_decline_not_rally(self):
        self.assertEqual(hunt.waves80([{'date':'1','low':1,'high':1},{'date':'2','low':1.8,'high':1.8}])['events'][0]['gain_pct'],80)
        self.assertFalse(hunt.waves80([{'date':'1','low':2,'high':2.1},{'date':'2','low':1,'high':1.1}])['events'])
        result=hunt.waves80([{'date':'1','low':1,'high':2}])
        self.assertFalse(result['events']);self.assertTrue(result['uncertain'])
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
        self.assertEqual(state,'pending')
    def test_one_same_day_rally_excludes(self):
        self.chart['candles'][0].update(low=1,high=1.1)
        self.chart['candles'][1].update(low=1.8,high=1.8)
        self.assertEqual(self.result()[0],'excluded')
    def test_live_price_can_prove_rally_from_old_low(self):
        self.chart['candles'][0].update(low=1.1,high=1.2)
        for candle in self.chart['candles'][1:]:candle.update(low=1.1,high=1.2)
        detail={}
        self.assertEqual(hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,self.chart,detail)[0],'excluded')
        self.assertGreater(detail['waves80'][0]['gain_pct'],80)
    def test_legacy_positive_proof_is_valid_for_single_rally(self):
        proof={'version':2,'effective_date':self.meta['effective_date'],'events':[{'gain_pct':149.42}]}
        self.assertEqual(hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,exclusion=proof)[0],'excluded')
    def test_no_daily_admission_on_chart_loss_staleness_or_partial_history(self):
        self.chart=None
        self.assertEqual(self.result()[0],'pending')
        self.setUp();self.chart['updated_at']='2026-10-01T00:00:00+00:00'
        self.assertEqual(self.result()[0],'pending')
        self.setUp();self.chart['candles']=self.chart['candles'][2:]
        self.assertEqual(self.result()[0],'pending')

    def test_old_extended_single_rally_remains_excluded_and_survive_restart(self):
        import storage,tempfile
        from unittest.mock import patch
        # The daily history misses both extended-hour spikes.
        candles=self.chart['candles']
        for index,(lo,hi) in zip([0,1],[(1,1.1),(1.8,1.8)]):
            candles[index].update(low=lo,high=hi)
        detail={}
        state,_=hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,self.chart,detail)
        self.assertEqual(state,'excluded');self.assertEqual(detail['waves80_count'],1)
        proofs={};snap={'diagnostics':[detail | {'symbol':'TEST'}]}
        self.assertTrue(hunt.remember_exclusions(snap,{'TEST':self.meta},proofs,self.at))
        self.assertFalse(hunt.remember_exclusions(snap,{'TEST':self.meta},proofs,self.at))
        with tempfile.TemporaryDirectory() as d,patch.object(storage,'DB_PATH',storage.Path(d)/'test.sqlite3'):
            storage.save({'hunt_rally_exclusions_v3':proofs})
            restored=storage.load(('hunt_rally_exclusions_v3',))['hunt_rally_exclusions_v3']
        for chart in [None,{},self.chart | {'updated_at':'2026-10-01T00:00:00+00:00'}]:
            state,_=hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,chart,exclusion=restored['TEST'])
            self.assertEqual(state,'excluded')
        # A verified new split invalidates the previous split's disqualification.
        self.meta['effective_date']='2026-10-01';self.h['effective_date']='2026-10-01'
        self.setUpChartForNewSplit= {**self.chart,'split_date':'2026-10-01','candles':[c for c in self.chart['candles'] if c['date']>='2026-10-01']}
        state,_=hunt.evaluate(self.meta,self.h,self.q,self.borrow,{},self.at,self.setUpChartForNewSplit,exclusion=restored['TEST'])
        self.assertEqual(state,'eligible')

    def test_wrong_split_chart_does_not_admit_or_record_exclusion(self):
        self.chart['split_date']='2026-08-01'
        self.assertEqual(self.result()[0],'pending')

    def test_evicted_candidates_are_verified_from_disk_without_ram_limit_filter(self):
        import asyncio,storage,tempfile
        from unittest.mock import patch
        universe={symbol:dict(self.meta,symbol=symbol) for symbol in ['A','B']}
        with tempfile.TemporaryDirectory() as d,patch.object(storage,'DB_PATH',storage.Path(d)/'test.sqlite3'):
            storage.save({'support_chart:A':self.chart,'support_chart:B':self.chart})
            result=asyncio.run(hunt.build_cached(universe,{s:self.h for s in universe},{s:self.q for s in universe},{s:self.borrow for s in universe},{},{},{},self.at))
            self.assertEqual({x['symbol'] for x in result['rows']},{'A','B'})

if __name__=='__main__':unittest.main()
