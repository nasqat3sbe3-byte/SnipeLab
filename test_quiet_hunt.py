import unittest
from datetime import datetime, timezone
import hunt
import quiet_hunt

class QuietHuntTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,28,19,0,tzinfo=timezone.utc)
        self.stamp=self.at.isoformat();self.dates=hunt.window(self.at)
        self.meta={'symbol':'TEST','effective_date':'2026-09-01'}
        self.h={'verified':True,'effective_date':'2026-09-01','split_day_4h_high':10,'updated_at':self.stamp}
        self.q={'price':6,'received_at':self.stamp,'market_timestamp':self.stamp}
        self.b={'available':14999,'received_at':self.stamp}
        candles=[]
        for i,day in enumerate(self.dates):
            for j,slot in enumerate(('04:00','09:30','13:30','16:00')):
                t=i*100+j;part={'time':t,'date':day,'local_time':day+' '+slot,'low':4,'high':5}
                candles.append({**part,'closed':True,'hourly_parts':[part]})
        self.chart={'updated_at':self.stamp,'split_date':'2026-09-01','candles':candles}
    def evaluate(self,rsi=29):return quiet_hunt.evaluate(self.meta,self.h,self.q,self.b,rsi,self.stamp,self.chart,self.at)[0]
    def test_boundaries(self):
        self.assertEqual(self.evaluate(),'eligible')
        self.assertEqual(self.evaluate(30),'excluded')
        self.b['available']=15000;self.assertEqual(self.evaluate(),'excluded')
        self.b['available']=14999;self.q['price']=6.01;self.assertEqual(self.evaluate(),'excluded')
        self.q['price']=1;self.assertEqual(self.evaluate(),'eligible')
    def test_one_session_and_uncertain_order(self):
        part=self.chart['candles'][1]['hourly_parts'][0];part['high']=6
        self.assertEqual(self.evaluate(),'excluded')
        part['high']=5;first=self.chart['candles'][0]['hourly_parts'][0];first['high']=6
        self.assertEqual(self.evaluate(),'pending')
    def test_missing_or_stale_data(self):
        self.chart['updated_at']='2026-10-28T12:00:00+00:00';self.assertEqual(self.evaluate(),'pending')
        self.chart['updated_at']=self.stamp;self.chart['candles']=self.chart['candles'][4:];self.assertEqual(self.evaluate(),'pending')
        self.chart=None;self.assertEqual(self.evaluate(),'pending')

if __name__=='__main__':unittest.main()
