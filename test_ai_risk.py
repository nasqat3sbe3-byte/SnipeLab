import tempfile
from pathlib import Path
import asyncio
import unittest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch
import httpx
import main as m

class AiRiskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache=dict(m._AI_RISK_CACHE);m._AI_RISK_CACHE.clear()
        self.pattern=m._AI_PATTERN_CACHE
    def tearDown(self):
        m._AI_RISK_CACHE.clear();m._AI_RISK_CACHE.update(self.cache);m._AI_PATTERN_CACHE=self.pattern
    def test_strict_thirty_day_window(self):
        today=date(2026,10,3)
        self.assertTrue(m._ai_window_event({'date':'2026-09-03'},today))
        self.assertFalse(m._ai_window_event({'date':'2026-09-02'},today))
        self.assertFalse(m._ai_window_event({'date':'2026-10-04'},today))
    def test_read_expires_old_blocks_and_failed_scans_never_pass(self):
        today=datetime.now(m.ZoneInfo('America/New_York')).date()
        m._AI_RISK_CACHE['X']={'version':m._AI_RISK_VERSION,'at':m.time.time(),'result':{'checked':False,'blocked':True,'events':[{'date':(today-timedelta(days=31)).isoformat()}]}}
        r=m._ai_cached_risk('X');self.assertFalse(r['blocked']);self.assertFalse(r['checked'])
        m._AI_RISK_CACHE['X']['result']['checked']=True
        self.assertTrue(m._ai_cached_risk('X')['checked'])
        self.assertFalse(m._ai_cached_risk('X',m.time.time()+3700)['checked'])
    def test_explicit_risk_not_sentiment(self):
        self.assertIsNotNone(m._ai_risk_kind('Company announces registered direct offering'))
        self.assertIsNotNone(m._ai_risk_kind('Company trial failed to meet the primary endpoint'))
        self.assertIsNone(m._ai_risk_kind('Analysts say shares may fall 20 percent'))
        self.assertIsNone(m._ai_risk_kind('Company emerges from chapter 11'))
        self.assertIsNone(m._ai_risk_kind('shelf registration','S-3'))
    async def test_endpoint_uses_cache_and_excludes_pending_stale_blocked(self):
        now=m.time.time();today=datetime.now(m.ZoneInfo('America/New_York')).date().isoformat()
        m._AI_PATTERN_CACHE={'candidates':[{'symbol':s,'score':90} for s in ['BAD','PENDING','STALE','OK']]}
        m._AI_RISK_CACHE.update({
          'BAD':{'version':m._AI_RISK_VERSION,'at':now,'result':{'checked':False,'events':[{'date':today,'kind':'طرح','verified':True,'evidence':'announced offering'}]}},
          'STALE':{'version':m._AI_RISK_VERSION,'at':now-4000,'result':{'checked':True,'events':[]}},
          'OK':{'version':m._AI_RISK_VERSION,'at':now,'result':{'checked':True,'events':[]}}})
        with patch.object(httpx.AsyncClient,'__aenter__',side_effect=AssertionError('HTTP request on fast endpoint')):
            result=await m.ai_patterns()
        self.assertEqual([x['symbol'] for x in result['picks']],['OK'])
        self.assertEqual(result['risk_pending'],['PENDING','STALE'])
        self.assertEqual(result['excluded_recent_risk'][0]['symbol'],'BAD')
    async def test_document_size_is_bounded(self):
        transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*101))
        async with httpx.AsyncClient(transport=transport) as c:
            with self.assertRaises(ValueError):await m._ai_risk_read(c,'https://example.test/doc',100)
    async def test_news_date_and_issuer_matching(self):
        xml=b'''<rss><channel><item><title>TEST announces public offering</title><pubDate>Fri, 02 Oct 2026 14:00:00 GMT</pubDate><link>https://example.test/new</link></item><item><title>TEST announces public offering</title><pubDate>Tue, 01 Sep 2026 14:00:00 GMT</pubDate></item><item><title>OTHER announces public offering</title><pubDate>Fri, 02 Oct 2026 14:00:00 GMT</pubDate></item></channel></rss>'''
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=xml))) as c:
            events,complete,error=await m._ai_news_risk(c,'TEST','Test Company',date(2026,10,3))
        self.assertTrue(complete);self.assertEqual(events,[])
    async def test_persistent_cache_survives_reload(self):
        entry={'version':m._AI_RISK_VERSION,'at':m.time.time(),'result':{'checked':True,'events':[]}}
        with tempfile.TemporaryDirectory() as folder,patch.object(m.storage,'DB_PATH',Path(folder)/'risk.sqlite3'):
            await asyncio.to_thread(m.storage.save,{'ai_risk_v2':{'entries':{'TEST':entry}}})
            restored=await asyncio.to_thread(m.storage.load,('ai_risk_v2',))
        m._AI_RISK_CACHE.update(restored['ai_risk_v2']['entries'])
        self.assertTrue(m._ai_cached_risk('TEST')['checked'])
    async def test_prospectus_requires_event_evidence(self):
        now=m.time.time();today=date(2026,10,3)
        submissions={'filings':{'recent':{'form':['424B5'],'filingDate':['2026-10-02'],'primaryDocument':['prospectus.htm'],'accessionNumber':['123-26-1']}}}
        for body,expected in [('Shelf registration. We may offer shares from time to time.',0),
                              ('On October 2, 2026, TEST announced pricing of its offering.',1)]:
            seen=[]
            def handler(request):
                seen.append(str(request.url))
                if 'submissions' in str(request.url):return httpx.Response(200,json=submissions)
                if str(request.url).endswith('index.json'):return httpx.Response(200,json={'directory':{'item':[]}})
                return httpx.Response(200,text=body)
            with patch.dict(m._AI_CIK_MAP,{'TEST':123}),patch.object(m,'_AI_CIK_AT',now),patch.dict(m._AI_FILING_CACHE,{},clear=True):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                    events,complete,error=await m._ai_sec_risk(c,'TEST',today)
            self.assertTrue(complete);self.assertEqual(len(events),expected);self.assertEqual(len(seen),3)
            if events:self.assertIn('evidence',events[0])
    def test_old_classifier_cache_cannot_block(self):
        today=datetime.now(m.ZoneInfo('America/New_York')).date().isoformat()
        m._AI_RISK_CACHE['LGCL']={'version':2,'at':m.time.time(),'result':{'checked':True,'events':[{'date':today,'kind':'طرح'}]}}
        risk=m._ai_cached_risk('LGCL')
        self.assertFalse(risk['blocked']);self.assertFalse(risk['checked'])
    async def test_source_failure_is_not_clean(self):
        async def news(*a):return [],True,None
        async def sec(*a):raise httpx.ConnectError('down')
        with patch.object(m,'_ai_news_risk',news),patch.object(m,'_ai_sec_risk',sec):
            result=await m._ai_scan_risk('TEST')
        self.assertFalse(result['checked']);self.assertFalse(result['blocked'])
        self.assertFalse(result['coverage']['sec']['complete'])

if __name__=='__main__':unittest.main()

