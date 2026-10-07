import unittest
from datetime import datetime, timedelta, timezone, date
from unittest.mock import patch
from history import calculate, split_day_4h_high, daily_history_start, stability_from_bars

def bars(n=15,start="2026-09-01"):
    d=datetime.fromisoformat(start)
    return [{"date":(d+timedelta(days=i)).date().isoformat(),"open":2,"high":3,"low":1,"close":2} for i in range(n)]

class HistoryTests(unittest.TestCase):
    def test_old_split_refresh_keeps_split_session_in_request(self):
        today=date(2026,10,6)
        start=daily_history_start("2026-05-11",today)
        # Reproduce the old rolling window losing May's first split bar.
        x=[{"date":"2026-05-11","open":5.22,"high":5.75,"low":4.52,"close":5.63},
           {"date":"2026-10-01","open":1.35,"high":1.39,"low":1.10,"close":1.12},
           {"date":"2026-10-02","open":1.12,"high":1.34,"low":1.12,"close":1.34},
           {"date":"2026-10-05","open":1.34,"high":1.55,"low":1.30,"close":1.55},
           {"date":"2026-10-06","open":1.50,"high":1.56,"low":1.33,"close":1.36}]
        old_start=today-timedelta(days=120)
        self.assertFalse(calculate("2026-05-11",[b for b in x if b['date']>=old_start.isoformat()])["verified"])
        with patch("history.datetime") as mock:
            mock.now.return_value=datetime(2026,10,6,19,57,tzinfo=timezone.utc)
            result=calculate("2026-05-11",[b for b in x if b['date']>=start.isoformat()])
            stability_from_bars(result,x)
        self.assertTrue(result["verified"])
        self.assertEqual(result["post_split_low"],1.10)
        self.assertEqual(result["post_split_low_date"],"2026-10-01")
        self.assertEqual(result["stability_sessions"],2)

    def test_post_split_extrema(self):
        x=bars()
        x[0]["high"]=5
        x[1]["low"]=0.7
        result=calculate("2026-09-01",x)
        self.assertTrue(result["verified"])
        self.assertEqual(result["split_day_high"],5)
        self.assertEqual(result["post_split_low"],0.7)
        self.assertEqual(result["post_split_high"],5)

    def test_pre_split_candles_excluded(self):
        x=bars()
        x[0]["high"]=100
        result=calculate("2026-09-02",x)
        self.assertEqual(result["post_split_high"],3)

    def test_future_first_bar_not_verified(self):
        x=bars(start="2026-09-10")
        result=calculate("2026-09-01",x)
        self.assertFalse(result["verified"])

    def test_top_does_not_pair_earlier_high_with_later_low(self):
        # Ten completed trading-session sample; highest bar occurs before the low.
        x=bars(n=15)
        for b in x:
            b["high"]=2.1
            b["low"]=2.0
        x[5]["high"]=100
        x[12]["low"]=1.0
        with patch("history.datetime") as mock:
            mock.now.return_value=datetime(2026,10,1,tzinfo=timezone.utc)
            result=calculate("2026-09-01",x)
        self.assertLess(result["top_10_gain_pct"],9000)
        self.assertGreaterEqual(result["top_10_high_date"],result["top_10_low_date"])

    def test_top_allows_observed_move_within_first_ten_sessions(self):
        x=bars(n=8)
        with patch("history.datetime") as mock:
            mock.now.return_value=datetime(2026,10,1,tzinfo=timezone.utc)
            result=calculate("2026-09-01",x)
        self.assertTrue(result["top_10_verified"])
        self.assertGreaterEqual(result["top_10_high_date"],result["top_10_low_date"])

class ExtendedHoursTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_day_4h_and_period_extrema(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz=ZoneInfo("America/New_York")
        days=[datetime(2026,9,21,h,tzinfo=tz) for h in (8,12,16)]
        days.append(datetime(2026,9,22,10,tzinfo=tz))
        class Response:
            def raise_for_status(self):pass
            def json(self):
                return {"chart":{"result":[{"meta":{"exchangeTimezoneName":"America/New_York"},
                    "timestamp":[int(d.timestamp()) for d in days],
                    "indicators":{"quote":[{"high":[5.876,6.47,5.5,7.1],
                                             "low":[5.0,4.8,4.5,3.2]}]}}]}}
        class Client:
            async def get(self,*args,**kwargs):return Response()
        x=await split_day_4h_high(Client(),"https://example.test/{symbol}","TEST","2026-09-21")
        self.assertEqual(x["split_day_4h_high"],6.47)
        self.assertEqual(x["extended_post_split_high"],7.1)
        self.assertEqual(x["extended_post_split_low"],3.2)
        self.assertFalse(x["extended_history_complete"])

if __name__=="__main__":unittest.main()

