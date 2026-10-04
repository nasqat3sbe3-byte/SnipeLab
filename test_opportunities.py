import unittest
from datetime import date,datetime,timezone
from opportunities import evaluate,advance

class CalendarTests(unittest.TestCase):
    def row(self,sessions=3):
        return {'symbol':'AAA','price':{'price':2},'borrow':{'available':200},'signal':{'history_verified':True,'post_split_low':1.8,'post_split_low_date':'2026-09-29','stability_sessions':sessions,'rsi_daily':25,'effective_distance_pct':11,'half_reached':True,'support_retest_status':'success','quote_freshness':{'status':'fresh'},'borrow_freshness':{'status':'fresh'}}}
    def test_weekend(self):
        self.assertEqual(advance(date(2026,10,2),1),date(2026,10,5))
    def test_holidays(self):
        self.assertEqual(advance(date(2026,11,25),1),date(2026,11,27))
        self.assertEqual(advance(date(2026,4,2),1),date(2026,4,6))
        self.assertEqual(advance(date(2026,12,31),1),date(2027,1,4))
    def test_conditional_date(self):
        r=self.row();v=evaluate(r,now=datetime(2026,10,4,12,tzinfo=timezone.utc))
        self.assertEqual(v['expected_date'],'2026-10-05');self.assertEqual(v['remaining_sessions'],1)
        self.assertEqual(r['signal']['stability_sessions'],3)
    def test_unknown_condition_no_date(self):
        r=self.row();r['signal']['support_retest_status']='waiting'
        v=evaluate(r,now=datetime(2026,10,4,12,tzinfo=timezone.utc));self.assertEqual(v['expected_date'],'2026-10-05')
    def test_failure_retained(self):
        r=self.row();now=datetime(2026,10,4,12,tzinfo=timezone.utc);old=evaluate(r,now=now)
        r['borrow']['available']=50000;v=evaluate(r,old,now)
        self.assertIsNotNone(v);self.assertIsNone(v['expected_date']);self.assertTrue(v['rescheduled']);self.assertEqual(v['history'][0]['old_date'],'2026-10-05')
    def test_new_low_restarts(self):
        r=self.row();now=datetime(2026,10,4,12,tzinfo=timezone.utc);old=evaluate(r,now=now)
        r['signal'].update(new_low_today=True,post_split_low=1.7,support_retest_status='waiting')
        v=evaluate(r,old,now);self.assertEqual(v['sessions'],0);self.assertEqual(v['expected_date'],'2026-10-08')
        self.assertIn('قاع جديد',v['history'][-1]['reason'])
    def test_stale_blocks(self):
        r=self.row();r['signal']['borrow_freshness']['status']='stale'
        self.assertIsNone(evaluate(r)['expected_date'])
    def test_ready_date_stable(self):
        r=self.row(4);old=evaluate(r,now=datetime(2026,10,5,22,tzinfo=timezone.utc))
        v=evaluate(r,old,datetime(2026,10,6,22,tzinfo=timezone.utc));self.assertEqual(v['expected_date'],old['expected_date'])
    def test_review_weekend_and_after_close(self):
        from opportunities import review_date
        self.assertEqual(review_date(datetime(2026,10,4,12,tzinfo=timezone.utc)),'2026-10-05')
        self.assertEqual(review_date(datetime(2026,10,5,18,tzinfo=timezone.utc)),'2026-10-05')
        self.assertEqual(review_date(datetime(2026,10,5,22,tzinfo=timezone.utc)),'2026-10-06')
    def test_data_issue_separate_from_condition(self):
        r=self.row();r['signal']['borrow_freshness']['status']='stale'
        v=evaluate(r)
        self.assertEqual(v['state'],'data_pending');self.assertIn('Available',v['data_issue'])
        self.assertIn('الثبات',v['waiting_label'])
    def test_change_labels(self):
        r=self.row();now=datetime(2026,10,4,12,tzinfo=timezone.utc)
        old=evaluate(r,now=now);r['signal']['new_low_today']=True
        v=evaluate(r,old,now);self.assertEqual(v['change_kind'],'تأجل')
    def test_last_session_available_during_weekend(self):
        from opportunities import usable_reading
        now=datetime(2026,10,4,14,tzinfo=timezone.utc)
        self.assertTrue(usable_reading({'status':'stale','timestamp':'2026-10-03T00:56:56+00:00'},now))
        self.assertFalse(usable_reading({'status':'stale','timestamp':'2026-09-30T05:03:46+00:00'},now))
        self.assertFalse(usable_reading({'status':'missing'},now))
    def test_old_reading_not_used_in_live_session(self):
        from opportunities import usable_reading
        self.assertFalse(usable_reading({'status':'stale','timestamp':'2026-10-02T20:56:56+00:00'},datetime(2026,10,5,15,tzinfo=timezone.utc)))
    def test_two_windows_and_support_plan(self):
        r=self.row(1);r['signal']['post_split_low_date']='2026-10-01'
        v=evaluate(r,now=datetime(2026,10,4,14,tzinfo=timezone.utc))
        self.assertEqual(v['early_date'],'2026-10-05')
        self.assertEqual(v['expected_date'],'2026-10-07')
        self.assertAlmostEqual(v['retest_zone']['high'],1.89)
        self.assertFalse(v['plan'][-1]['met'])
    def test_waiting_retest_has_no_early_date(self):
        r=self.row(3);r['signal']['support_retest_status']='waiting'
        self.assertIsNotNone(evaluate(r,now=datetime(2026,10,4,14,tzinfo=timezone.utc))['early_date'])
    def test_diary_independent_of_rsi_half_and_distance(self):
        r=self.row();r['borrow']['available']=19999
        r['signal'].update(rsi_daily=90,half_reached=False,effective_distance_pct=80,support_retest_status='waiting')
        v=evaluate(r,now=datetime(2026,10,4,14,tzinfo=timezone.utc))
        self.assertTrue(v['active']);self.assertEqual(v['expected_date'],'2026-10-05')
        r['borrow']['available']=20000
        self.assertIsNone(evaluate(r))
    def test_filter_preserves_history_and_returns(self):
        import opportunities as o
        now=datetime(2026,10,4,14,tzinfo=timezone.utc);r=self.row();v=evaluate(r,now=now)
        r['borrow']['available']=20000;hidden=evaluate(r,v,now)
        old=o.ROWS.copy()
        try:
            o.ROWS.clear();o.ROWS['AAA']=hidden
            self.assertEqual(o.payload()['rows'],[])
            r['borrow']['available']=19000
            o.ROWS['AAA']=evaluate(r,hidden,now)
            self.assertEqual(len(o.payload()['rows']),1)
            self.assertTrue(o.ROWS['AAA']['history'])
        finally:o.ROWS.clear();o.ROWS.update(old)
    def test_live_low_kept_until_history_catches_up(self):
        r=self.row();r['signal'].update(new_low_today=True,effective_low=1.7)
        now=datetime(2026,10,4,14,tzinfo=timezone.utc)
        v=evaluate(r,now=now);self.assertEqual(v['support'],1.7)
        r['signal'].update(new_low_today=False,effective_low=1.8)
        again=evaluate(r,v,now)
        self.assertEqual(again['support'],1.7);self.assertEqual(again['sessions'],0)
    def test_today_changes_and_break(self):
        import opportunities as o
        now=datetime(2026,10,4,14,tzinfo=timezone.utc);r=self.row(3)
        old=evaluate(r,now=now);r['signal']['support_retest_status']='failed'
        v=evaluate(r,old,now);self.assertEqual(v['history'][-1]['kind'],'support_broken')
        previous=o.ROWS.copy()
        try:
            o.ROWS.clear();o.ROWS['AAA']=v
            self.assertEqual(o.daily_changes(now)['counts']['support_broken'],1)
            self.assertEqual(o.daily_changes(datetime(2026,10,5,14,tzinfo=timezone.utc))['events'],[])
        finally:o.ROWS.clear();o.ROWS.update(previous)
if __name__=='__main__':unittest.main()
