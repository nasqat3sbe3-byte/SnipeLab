import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from history import calculate

def bars(n=15,start="2026-09-01"):
    d=datetime.fromisoformat(start)
    return [{"date":(d+timedelta(days=i)).date().isoformat(),"open":2,"high":3,"low":1,"close":2} for i in range(n)]

class HistoryTests(unittest.TestCase):
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

    def test_top_requires_ten_completed_sessions(self):
        x=bars(n=8)
        with patch("history.datetime") as mock:
            mock.now.return_value=datetime(2026,10,1,tzinfo=timezone.utc)
            result=calculate("2026-09-01",x)
        self.assertFalse(result["top_10_verified"])

if __name__=="__main__":unittest.main()
