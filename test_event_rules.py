import unittest
from datetime import datetime,timezone,timedelta
from event_rules import borrow_events,ready_event,worker_health

class EventTests(unittest.TestCase):
    def test_zero_transition_only_once(self):
        first=borrow_events("RETO",{"available":250000},{"available":0})
        self.assertEqual([e[1] for e in first],["available_down","available_zero"])
        self.assertEqual(borrow_events("RETO",{"available":0},{"available":0}),[])
        self.assertEqual(borrow_events("RETO",None,{"available":0}),[])
    def test_ready_transition_only(self):
        self.assertIsNotNone(ready_event("RETO",False,True,.17,0))
        self.assertIsNone(ready_event("RETO",True,True,.17,0))
        self.assertIsNone(ready_event("RETO",True,False,.17,0))
    def test_worker_staleness(self):
        now=datetime(2026,9,22,16,0,tzinfo=timezone.utc)
        self.assertEqual(worker_health(now,(now-timedelta(seconds=10)).isoformat(),180)["state"],"active")
        self.assertEqual(worker_health(now,(now-timedelta(seconds=200)).isoformat(),180)["state"],"stale")
        self.assertEqual(worker_health(now,None,180)["state"],"waiting")

if __name__=="__main__":unittest.main()
