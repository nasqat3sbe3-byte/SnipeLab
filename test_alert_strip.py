import unittest
from datetime import datetime, timezone, timedelta
from event_rules import strip_items
import quiet_hunt

class StripTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        quiet_hunt.ALERTS.clear()
    def event(self,kind,symbol='TEST',hours=0):
        return dict(kind=kind,symbol=symbol,at=(self.now-timedelta(hours=hours)).isoformat())
    def test_merge_and_only_allowed_fresh_events(self):
        events=[self.event('ready'),self.event('available_zero'),self.event('price_25','OTHER'),self.event('ready','OLD',25)]
        result=strip_items(events,[],{},self.now)
        self.assertEqual(len(result),1)
        self.assertIn('دخل الأجهز',result[0]['text'])
        self.assertIn('المتاح صفر',result[0]['text'])
    def test_macro_reserved_and_limit(self):
        calendar={'status':'ok','events':[{'title':'CPI m/m','time':(self.now+timedelta(hours=1)).isoformat()}]}
        result=strip_items([self.event('ready',str(i)) for i in range(10)],[],calendar,self.now)
        self.assertEqual(len(result),3)
        self.assertEqual(result[-1]['title'],'CPI m/m')
    def test_baseline_pending_and_repeated_readings(self):
        memory={}
        for state in ['eligible','eligible','pending','eligible']:
            quiet_hunt.record_transition(memory,'TEST',state,None,self.now)
        self.assertEqual(quiet_hunt.ALERTS,[])
        for state in ['excluded','pending','eligible','eligible']:
            quiet_hunt.record_transition(memory,'TEST',state,None,self.now)
        self.assertEqual(len(quiet_hunt.ALERTS),1)
    def test_empty_and_unavailable(self):
        self.assertEqual(strip_items([],[],{'status':'unavailable'},self.now),[])
