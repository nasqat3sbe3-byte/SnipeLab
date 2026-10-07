import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import focus as f
AT=datetime(2026,10,7,20,0,tzinfo=timezone.utc)
def base_bars():
    dates=['2026-09-29','2026-09-30','2026-10-01','2026-10-02','2026-10-05','2026-10-06']
    return [{'date':d,'low':1 if i<3 else 1.01+(i-3)*.01,'high':1.5 if i<3 else 1.12,
             'close':1.1 if i<3 else 1.05+(i-3)*.02} for i,d in enumerate(dates)]
def row(sym='TEST',**extra):
    return {'symbol':sym,'price':1.1,'support':1,'available':1000,'rsi':25,'sessions':3,
            'verified':True,'retest':True,'retest_day':AT.date().isoformat(),'confirmation_at':AT.isoformat(),
            'price_at':AT.isoformat(),'borrow_at':AT.isoformat(),'pattern_bars':base_bars(),**extra}
def risk(sym):return {'checked':True,'blocked':False}
class FocusTests(unittest.TestCase):
    def setUp(self):
        self.memory=patch.dict(f.MEMORY,{},clear=True);self.memory.start()
        self.events=patch.object(f,'EVENTS',[]);self.events.start()
    def tearDown(self):self.memory.stop();self.events.stop()
    def test_five_max_no_fillers(self):
        result=f.update([row(str(i)) for i in range(8)],risk,AT)
        self.assertEqual(len(result['picks']),5)
        self.assertIn('احتمال صعود',result['note'])
        self.assertEqual(f.update([row(retest=False,pattern_bars=[])],risk,AT)['picks'],[])
    def test_news_gate(self):
        for r in ({'checked':False},{'blocked':True,'checked':True}):
            self.assertEqual(f.update([row()],lambda s:r,AT)['picks'],[])
        self.assertTrue(f.update([row()],risk,AT)['picks'])
    def test_stale_sources(self):
        old=(AT-timedelta(days=3)).isoformat()
        for k in ('price_at','borrow_at'):
            self.assertFalse(f.update([row(**{k:old})],risk,AT)['picks'])
    def test_zero_is_valid_missing_is_not_zero(self):
        self.assertTrue(f.update([row(available=0)],risk,AT)['picks'])
        self.assertFalse(f.update([row(available=None)],risk,AT)['picks'])
        self.assertFalse(f.update([row(price=5)],risk,AT)['picks'])
    def test_break_reset_and_reclaim_memory(self):
        f.update([row()],risk,AT)
        broken=f.update([row(price=.8,broken=True)],risk,AT)
        self.assertFalse(broken['picks']);self.assertEqual(broken['watching'][0]['sessions'],0)
        self.assertFalse(f.update([row(support=.8,price=.85,retest=False,sessions=2)],risk,AT)['picks'])
        recovered=f.update([row(support=.9,price=1.05,retest=False,sessions=2)],risk,AT)
        self.assertTrue(recovered['picks'])
        self.assertIn('استرجع الدعم المكسور',recovered['picks'][0]['reasons'])
    def test_distinct_borrow_readings(self):
        for i in range(3):f.update([row(available=3000)],risk,AT)
        self.assertEqual(len(f.MEMORY['TEST']['borrow_samples']),1)
        for i,av in enumerate((2000,1000),1):
            t=AT+timedelta(minutes=i)
            result=f.update([row(available=av,borrow_at=t.isoformat(),price_at=t.isoformat())],risk,t)
        self.assertIn('Available يتناقص عبر 3 قراءات',result['picks'][0]['reasons'])
    def test_events_no_repeat_and_remember_departures(self):
        f.update([row()],risk,AT);count=len(f.EVENTS)
        f.update([row()],risk,AT);self.assertEqual(len(f.EVENTS),count)
        result=f.update([],risk,AT)
        self.assertEqual(result['remembered'][0]['symbol'],'TEST')
        self.assertFalse(result['remembered'][0]['eligible'])
    def test_ancient_retest_not_current_improvement(self):
        self.assertFalse(f.update([row(retest_day='2026-08-01',pattern_bars=[])],risk,AT)['picks'])
    def test_same_support_reclaimed_after_break(self):
        f.update([row(price=.9,broken=True)],risk,AT)
        self.assertTrue(f.update([row(price=1.05,retest=False)],risk,AT)['picks'])
    def test_higher_low_requires_two_sessions(self):
        self.assertFalse(f.update([row(retest=False,higher_low=True,sessions=1)],risk,AT)['picks'])
        self.assertTrue(f.update([row(retest=False,higher_low=True,sessions=2)],risk,AT)['picks'])
if __name__=='__main__':unittest.main()

class IndependentPoolTests(unittest.TestCase):
    def test_each_market_has_five_and_independent_ranking(self):
        with patch.dict(f.MEMORY,{},clear=True),patch.object(f,'EVENTS',[]):
            splits=[row('S'+str(i),market='split',source='أسهم التقسيم') for i in range(7)]
            floats=[row('F'+str(i),market='low_float',source='الفري فلوت المنخفض') for i in range(7)]
            result=f.update(splits+floats,risk,AT)
            self.assertEqual(len(result['picks']),5)
            for market,prefix in [('split','S'),('low_float','F')]:
                pool=result['pools'][market]
                self.assertEqual(len(pool['picks']),5)
                for section in ('picks','watching','events'):
                    self.assertTrue(all(x['symbol'].startswith(prefix) for x in pool[section]))
            result=f.update([row('S0',broken=True,market='split')]+floats,risk,AT)
            self.assertEqual(len(result['pools']['low_float']['picks']),5)
            self.assertEqual(result['pools']['split']['picks'],[])
    def test_legacy_low_float_memory_stays_in_its_pool(self):
        with patch.dict(f.MEMORY,{'F':{'first_seen':AT.isoformat(),'last_seen':AT.isoformat(),
            'last_result':{'symbol':'F','source':'الفري فلوت المنخفض'}}},clear=True),patch.object(f,'EVENTS',[]):
            result=f.update([],risk,AT)
            self.assertEqual(result['remembered'],[])
            self.assertEqual(result['pools']['low_float']['remembered'][0]['symbol'],'F')

class PatternTests(unittest.TestCase):
    def test_base_can_be_monitored_before_breakout_without_retest(self):
        result=f.evaluate(row(retest=False),{},risk('TEST'),AT)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['stage'],'setup')
        self.assertFalse(result['pattern']['confirmed'])
        self.assertEqual(result['pattern']['name'],'base')
    def test_new_low_and_price_chase_are_not_base_matches(self):
        bars=base_bars();bars[-1]['low']=.8
        self.assertIsNone(f.pattern_state(bars,1.1,AT)['name'])
        self.assertIsNone(f.pattern_state(base_bars(),1.5,AT)['name'])
    def test_recovery_requires_a_later_close_not_same_candle_wick(self):
        bars=base_bars()
        for b in bars:b.update(low=1.9,high=2.1,close=2)
        bars[-2].update(low=1.2,high=2.1,close=1.4)
        bars[-1].update(low=1.3,high=2.2,close=2.05)
        p=f.pattern_state(bars,2.05,AT)
        self.assertEqual(p['name'],'recovery');self.assertTrue(p['confirmed'])
        result=f.evaluate(row(price=2.05,support=1.8,sessions=1,retest=False,pattern_bars=bars),{},risk('TEST'),AT)
        self.assertTrue(result['eligible'])
        bars[-1]['close']=1.5
        self.assertIsNone(f.pattern_state(bars,2.05,AT)['name'])
    def test_future_incomplete_old_and_missing_bars_do_not_select(self):
        for kind in ['future','old','missing']:
            bars=base_bars()
            if kind=='future':bars[-1]['date']='2026-10-08'
            elif kind=='old':
                for i,b in enumerate(bars):b['date']='2026-08-'+str(i+10)
            else:bars[-1]['low']=None
            self.assertIsNone(f.pattern_state(bars,1.1,AT)['name'])
    def test_cache_coverage_and_split_boundaries(self):
        candles=[]
        for i,b in enumerate(base_bars()):
            for j,slot in enumerate(['09:30','13:30']):
                candles.append({**b,'time':i*2+j,'local_time':b['date']+' '+slot,'closed':True,'samples':4 if j==0 else 3})
        self.assertEqual(len(f.pattern_bars(candles,'2026-09-29',AT)),6)
        self.assertEqual(len(f.pattern_bars(candles,'2026-10-02',AT)),3)
        candles[-1]['closed']=False
        self.assertEqual(f.pattern_bars(candles,None,AT),[])
    def test_prefix_cannot_use_later_rally(self):
        bars=base_bars()
        before=f.pattern_state(bars,1.1,AT)
        later=bars+[{'date':'2026-10-08','low':1.1,'high':5,'close':4}]
        self.assertIsNone(f.pattern_state(later,1.1,AT)['name'])
        self.assertEqual(before,f.pattern_state(bars,1.1,AT))
