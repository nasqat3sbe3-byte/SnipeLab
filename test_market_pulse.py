import unittest
from datetime import datetime, timezone, timedelta
from market_pulse import build, parse_chart

NOW=datetime(2026,10,27,22,tzinfo=timezone.utc)
# Observed exchange sessions, with a deliberately omitted holiday/weekend gap.
DAYS=['2026-10-08','2026-10-09','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23','2026-10-26','2026-10-27']
def record(bars): return {'bars':bars,'fetched_at':NOW.timestamp()}
def bars(): return [dict(date=d,high=10,close=10,volume=100) for d in DAYS]
def fixture(n=12):
    cache={'_calendar':record(bars())}; groups={}
    for i in range(n): cache[str(i)]=record(bars());groups[str(i)]='أسهم التقسيم'
    return cache,groups
class PulseTests(unittest.TestCase):
    def test_previous_close_and_multiple_moves(self):
        c,g=fixture(); c['0']['bars'][-1].update(high=20,close=15,volume=300)
        c['0']['bars'][-3].update(high=15,close=10)
        p=build(c,g,NOW); r=p['rows'][0]
        self.assertEqual(r['gain'],100);self.assertEqual(r['close_gain'],50)
        self.assertEqual(len(r['moves']),2);self.assertEqual(p['counts'],{'10':1,'25':1,'50':1,'100':1})
        self.assertEqual(r['volume_ratio'],3)
    def test_shared_window_expires_old_moves(self):
        c,g=fixture(); c['0']['bars'][3]['high']=30
        self.assertEqual(build(c,g,NOW)['rows'],[])
        c['0']['bars'][4]['high']=30
        r=build(c,g,NOW)['rows'][0]
        self.assertEqual(r['since'],9)
    def test_stale_data_never_counts_as_zero_or_healthy(self):
        c,g=fixture();c['0']['fetched_at']-=2000
        p=build(c,g,NOW);self.assertEqual(p['covered'],11)
        c['_calendar']['fetched_at']-=2000
        self.assertEqual(build(c,g,NOW)['status'],'stale')
    def test_missing_session_excluded_from_comparison_cohort(self):
        c,g=fixture();del c['0']['bars'][-3]
        self.assertEqual(build(c,g,NOW)['covered'],11)
    def test_insufficient_coverage_suspends_judgment(self):
        c,g=fixture();
        for s in list(g)[:8]:del c[s]
        self.assertEqual(build(c,g,NOW)['status'],'collecting')
    def test_isolated_runner_does_not_produce_broad_signal(self):
        c,g=fixture();c['0']['bars'][-1]['high']=40
        self.assertEqual(build(c,g,NOW)['label'],'نشاط محدود')
    def test_weekend_uses_observed_sessions(self):
        c,g=fixture(); weekend=datetime(2026,10,24,22,tzinfo=timezone.utc)
        for v in c.values():
            v['bars']=[b for b in v['bars'] if b['date']<='2026-10-23'];v['fetched_at']=weekend.timestamp()
        p=build(c,g,weekend)
        self.assertEqual(p['as_of'],'2026-10-23');self.assertFalse(p['provisional'])
    def test_new_listing_can_appear_without_distorting_breadth(self):
        c,g=fixture(); c['0']['bars']=c['0']['bars'][-3:]
        c['0']['bars'][-1]['high']=20
        p=build(c,g,NOW)
        self.assertEqual(p['covered'],11)
        self.assertEqual(p['rows'][0]['symbol'],'0')
        self.assertEqual(p['counts']['100'],0)
    def test_bad_ohlc_and_nan_ignored(self):
        self.assertEqual(parse_chart({'timestamp':[1], 'indicators':{'quote':[{'high':[float('nan')],'close':[10],'volume':[100]}]}}),[])
if __name__=='__main__':unittest.main()
