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
        v=evaluate(r,now=datetime(2026,10,4,12,tzinfo=timezone.utc));self.assertIsNone(v['expected_date'])
    def test_failure_retained(self):
        r=self.row();now=datetime(2026,10,4,12,tzinfo=timezone.utc);old=evaluate(r,now=now)
        r['borrow']['available']=50000;v=evaluate(r,old,now)
        self.assertIsNotNone(v);self.assertIsNone(v['expected_date']);self.assertTrue(v['rescheduled']);self.assertEqual(v['history'][0]['old_date'],'2026-10-05')
    def test_new_low_restarts(self):
        r=self.row();now=datetime(2026,10,4,12,tzinfo=timezone.utc);old=evaluate(r,now=now)
        r['signal'].update(new_low_today=True,post_split_low=1.7,support_retest_status='waiting')
        v=evaluate(r,old,now);self.assertEqual(v['sessions'],0);self.assertIsNone(v['expected_date'])
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
        v=evaluate(r,old,now);self.assertEqual(v['change_kind'],'عُلّق الموعد')
    def test_last_session_available_during_weekend(self):
        from opportunities import usable_reading
        now=datetime(2026,10,4,14,tzinfo=timezone.utc)
        self.assertTrue(usable_reading({'status':'stale','timestamp':'2026-10-03T00:56:56+00:00'},now))
        self.assertFalse(usable_reading({'status':'stale','timestamp':'2026-09-30T05:03:46+00:00'},now))
        self.assertFalse(usable_reading({'status':'missing'},now))
    def test_old_reading_not_used_in_live_session(self):
        from opportunities import usable_reading
        self.assertFalse(usable_reading({'status':'stale','timestamp':'2026-10-02T20:56:56+00:00'},datetime(2026,10,5,15,tzinfo=timezone.utc)))
if __name__=='__main__':unittest.main()
