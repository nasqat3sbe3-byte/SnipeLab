import unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
import focus_study as s
import focus
AT=datetime(2026,10,7,22,0,tzinfo=timezone.utc)
def bars(rally=False):
    result=[{'date':(AT.date()-timedelta(days=20-i)).isoformat(),'low':1,'high':1.2,'close':1.1} for i in range(20)]
    if rally:result[-2].update(high=2.4,close=2.1)
    return result
class StudyTests(unittest.TestCase):
    def test_studied_selection_keeps_news_and_break_gates(self):
        from test_focus import row,risk,base_bars
        b=base_bars();v=s.vector(b)
        model={'status':'ready','scales':[1]*8,'positives':[{'symbol':'REFERENCE','vector':v,
                'gain_pct':120,'observed_at':'2026-09-20'}],
               'negatives':[{'symbol':'COMPARISON','vector':[3]*8}]}
        with patch.dict(focus.MODELS,{'split':model},clear=True):
            candidate=row(study_bars=b,retest=False,sessions=0,price_at=AT.isoformat(),borrow_at=AT.isoformat())
            result=focus.evaluate(candidate,{},risk('TEST'),AT)
            self.assertTrue(result['eligible']);self.assertTrue(result['similarity']['matched'])
            self.assertEqual(result['pattern']['name'],'observed_similarity')
            for change,news in [({'broken':True},risk('TEST')),({}, {'checked':False}),({}, {'checked':True,'blocked':True})]:
                self.assertFalse(focus.evaluate({**candidate,**change},{},news,AT)['eligible'])
            # No price chasing merely because the old feature vector matches.
            self.assertFalse(focus.evaluate({**candidate,'price':1.5},{},risk('TEST'),AT)['eligible'])
    def test_features_do_not_use_future_target(self):
        b=bars(True);before=s.vector(b[:-2]);b[-2]['high']=8
        self.assertEqual(s.vector(b[:-2]),before)
        self.assertEqual(s.build({'A':b},AT)['positives'][0]['vector'],before)
    def test_same_session_order_is_not_invented(self):
        b=bars();b[-1].update(low=.1,high=1.2,close=1)
        self.assertIsNone(s.recent_episode(b))
    def test_only_recent_peak_with_six_prior_sessions(self):
        b=bars(True)
        self.assertTrue(s.build({'A':b},AT)['positives'])
        b[-2].update(high=1.2,close=1.1);b[2].update(high=2.4,close=2.1)
        self.assertFalse(s.build({'A':b},AT)['positives'])
        self.assertFalse(s.build({'A':bars(True)[-5:]},AT)['positives'])
    def test_comparisons_need_complete_ten_session_outcome(self):
        self.assertFalse(s.build({'A':bars()[:15]},AT)['negatives'])
        negatives=s.build({'A':bars()},AT)['negatives']
        self.assertTrue(negatives)
        for r in negatives:self.assertGreater(r['resolved_at'],r['observed_at'])
    def test_never_match_same_symbol(self):
        model=s.build({'A':bars(True),'B':bars()},AT)
        self.assertIsNone(s.nearest(model,model['positives'][0]['vector'],'A'))
    def test_sparse_bad_future_and_old_inputs(self):
        self.assertIsNone(s.vector(bars()[:5]))
        bad=bars();bad[-1]['low']=None
        self.assertEqual(s.clean_bars(bad,AT),[])
        future=bars()+[{'date':'2026-10-08','low':1,'high':100,'close':99}]
        self.assertEqual(s.clean_bars(future,AT),bars())
        old=[{**r,'date':'2026-08-'+r['date'][-2:]} for r in bars(True)]
        self.assertEqual(s.build({'A':old},AT)['status'],'insufficient')
    def test_independent_market_models_and_research_only_rows(self):
        with patch.dict(focus.MODELS,{},clear=True),patch.object(focus,'MODEL_AT',None):
            focus.calibrate([{'symbol':'S','market':'split','verified':True,'study_bars':bars(True)},
                             {'symbol':'F','market':'low_float','verified':True,'study_bars':bars()}],AT)
            self.assertEqual([r['symbol'] for r in focus.MODELS['split']['positives']],['S'])
            self.assertEqual(focus.MODELS['low_float']['positives'],[])
            self.assertIsNone(focus.evaluate({'symbol':'S','study_only':True},{},{},AT))
            self.assertIsNone(focus.evaluate({'symbol':'S','already_rallied':True},{},{},AT))
    def test_insufficient_study_cannot_claim_match(self):
        model=s.build({'A':bars(True)},AT)
        self.assertIsNone(s.match(model,bars(),'B',1.1,AT))
        self.assertFalse(model['validation']['predictive_performance_established'])
    def test_bounds_and_no_probability_output(self):
        model=s.build({**{'P'+str(i):bars(True) for i in range(5)},**{'N'+str(i):bars() for i in range(5)}},AT)
        self.assertEqual(model['status'],'ready')
        self.assertLessEqual(len(model['negatives']),20)
        match=s.match(model,bars(),'NEW',1.1,AT)
        self.assertIsNotNone(match)
        self.assertNotIn('probability',match)
        self.assertLessEqual(len(match['closest']),3)
        self.assertFalse(s.match(model,bars(),'NEW',2,AT)['matched'])
