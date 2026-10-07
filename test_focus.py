import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import focus as f
AT=datetime(2026,10,7,20,0,tzinfo=timezone.utc)
def row(sym='TEST',**extra):
    return {'symbol':sym,'price':1.1,'support':1,'available':1000,'rsi':25,'sessions':3,
            'verified':True,'retest':True,'retest_day':AT.date().isoformat(),'confirmation_at':AT.isoformat(),
            'price_at':AT.isoformat(),'borrow_at':AT.isoformat(),**extra}
def risk(sym):return {'checked':True,'blocked':False}
class FocusTests(unittest.TestCase):
    def setUp(self):
        self.memory=patch.dict(f.MEMORY,{},clear=True);self.memory.start()
        self.events=patch.object(f,'EVENTS',[]);self.events.start()
    def tearDown(self):self.memory.stop();self.events.stop()
    def test_five_max_no_fillers(self):
        result=f.update([row(str(i)) for i in range(8)],risk,AT)
        self.assertEqual(len(result['picks']),5)
        self.assertIn('ليست احتمال',result['note'])
        self.assertEqual(f.update([row(retest=False)],risk,AT)['picks'],[])
    def test_news_gate(self):
        for r in ({'checked':False},{'blocked':True,'checked':True}):
            self.assertEqual(f.update([row()],lambda s:r,AT)['picks'],[])
        self.assertTrue(f.update([row()],risk,AT)['picks'])
    def test_stale_sources(self):
        old=(AT-timedelta(days=3)).isoformat()
        for k in ('price_at','borrow_at','confirmation_at'):
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
        self.assertFalse(f.update([row(retest_day='2026-08-01')],risk,AT)['picks'])
    def test_same_support_reclaimed_after_break(self):
        f.update([row(price=.9,broken=True)],risk,AT)
        self.assertTrue(f.update([row(price=1.05,retest=False)],risk,AT)['picks'])
    def test_higher_low_requires_two_sessions(self):
        self.assertFalse(f.update([row(retest=False,higher_low=True,sessions=1)],risk,AT)['picks'])
        self.assertTrue(f.update([row(retest=False,higher_low=True,sessions=2)],risk,AT)['picks'])
if __name__=='__main__':unittest.main()
