import unittest
from datetime import date
from risk_evidence import dated_risk_evidence

def classify(text):
    return 'طرح' if any(x in text.lower() for x in ('private placement', 'public offering', 'pricing of its offering')) else None

class EvidenceTests(unittest.TestCase):
    def scan(self, text):
        return dated_risk_evidence(text,date(2026,10,7),date(2026,10,7),classify)
    def test_lgcl_results_do_not_create_october_offering(self):
        self.assertIsNone(self.scan('Lucas GC announces 1H 2026 financial results. Financing activities for the six months ended June 30, 2026 included sale of ordinary shares through private placement.'))
        self.assertIsNone(self.scan('Financial results. On February 10, 2026, the Company entered into a private placement agreement.'))
    def test_boilerplate_and_recycled_news(self):
        self.assertIsNone(self.scan('The company previously announced a public offering.'))
        self.assertIsNone(self.scan('Our registration statement incorporates by reference the public offering announced in June.'))
        self.assertIsNone(self.scan('We may offer shares through a public offering from time to time.'))
    def test_real_recent_offering_retains_date_and_evidence(self):
        result=self.scan('On September 25, 2026, the Company announced pricing of its offering.')
        self.assertEqual(result['date'],'2026-09-25')
        self.assertEqual(result['published_date'],'2026-10-07')
        self.assertTrue(result['verified']);self.assertIn('pricing',result['evidence'])
    def test_results_can_report_a_real_recent_event(self):
        result=self.scan('Financial results. On October 6, 2026, the Company entered into a private placement.')
        self.assertEqual(result['date'],'2026-10-06')
    def test_unknown_event_date_in_results_does_not_block(self):
        self.assertIsNone(self.scan('Financial results. The Company completed a private placement.'))
    def test_future_and_old_events(self):
        self.assertIsNone(self.scan('On October 9, 2026, the Company announced a public offering.'))
        self.assertIsNone(self.scan('On August 10, 2026, the Company announced a public offering.'))
    def test_recent_announcement(self):
        self.assertEqual(self.scan('Company announces public offering.')['date'],'2026-10-07')
    def test_cancellations_negations_and_incomplete_old_dates(self):
        for text in ('Company announces termination of its public offering.',
                     'Company has not announced a public offering.',
                     'Company announced a private placement in June.',
                     'Company completed a private placement last year.'):
            self.assertIsNone(self.scan(text))

if __name__=='__main__':unittest.main()
