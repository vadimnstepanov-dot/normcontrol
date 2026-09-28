import io,json,ssl,tempfile,unittest,urllib.error,uuid
from unittest.mock import patch
from knowledge_v2.bridge import Bridge
from knowledge_v2.store import KnowledgeStore


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bridge=Bridge(KnowledgeStore(self.tmp.name),'https://portal.example/api/v2','x'*32)

    def test_idempotent_delivery_retries_identical_compact_utf8_body(self):
        bodies=[]
        def open_request(request,**kwargs):
            bodies.append(request.data)
            if len(bodies)==1:raise urllib.error.URLError(TimeoutError('temporary'))
            return io.BytesIO(b'{"accepted":true}')
        with patch('knowledge_v2.bridge.urllib.request.urlopen',side_effect=open_request),patch('knowledge_v2.bridge.time.sleep'):
            self.assertTrue(self.bridge.http('/worker/analysis/',{'quote':'Точная цитата'})['accepted'])
        self.assertEqual(bodies[0],bodies[1]);self.assertIn('Точная цитата'.encode(),bodies[0])
        self.assertEqual(json.loads(bodies[0]),{'quote':'Точная цитата'})

    def test_claim_never_retries_after_uncertain_reply(self):
        with patch('knowledge_v2.bridge.urllib.request.urlopen',side_effect=urllib.error.URLError('offline')) as request,patch('knowledge_v2.bridge.time.sleep') as sleep:
            with self.assertRaises(urllib.error.URLError):self.bridge.http('/worker/claim/',{})
            self.assertEqual(request.call_count,1);sleep.assert_not_called()

    def test_denied_stale_and_certificate_failures_are_not_retried(self):
        errors=[urllib.error.HTTPError('https://portal',code,'denied',{},None) for code in [401,403,409]]
        errors.append(urllib.error.URLError(ssl.SSLCertVerificationError('untrusted')))
        for error in errors:
            with self.subTest(error=type(error).__name__),patch('knowledge_v2.bridge.urllib.request.urlopen',side_effect=error) as request,patch('knowledge_v2.bridge.time.sleep'):
                with self.assertRaises(type(error)):self.bridge.http('/worker/events/',{})
                self.assertEqual(request.call_count,1)

    def test_outage_is_bounded_and_keeps_original_exception(self):
        error=urllib.error.URLError('offline')
        with patch('knowledge_v2.bridge.urllib.request.urlopen',side_effect=error) as request,patch('knowledge_v2.bridge.time.sleep'):
            with self.assertRaises(urllib.error.URLError) as caught:self.bridge.http('/worker/events/',{})
            self.assertIs(caught.exception,error);self.assertEqual(request.call_count,5)

    def test_ready_partial_analysis_survives_failed_remote_delivery(self):
        from knowledge_v2.semantic import VERSION
        claim=dict(command_id=str(uuid.uuid4()),kind='source.analyze',lease='same-lease',
                   payload=dict(set_id='set',source_id=str(uuid.uuid4()),sha256='a'*64,extractor_version=VERSION))
        ready=dict(source_sha256='a'*64,projection=[dict(id='card',locator='p1',state='needs_review',validation={})],
                   summary=dict(kind='source.analyzed',set_id='set',semantic_completeness='partial'))
        failed=[False]
        def transport(path,payload):
            if path=='/worker/claim/':return {'command':claim}
            if path=='/worker/analysis/' and not failed[0]:
                failed[0]=True;raise urllib.error.URLError('temporary')
            return {'accepted':True}
        self.bridge.transport=transport;self.bridge.analysis_client=object()
        with patch('knowledge_v2.bridge.analyze_source',return_value=ready) as analyze:
            with self.assertRaises(urllib.error.URLError):self.bridge.once()
            self.assertEqual(self.bridge.analysis_delivery(claim),ready)
            self.assertTrue(self.bridge.once())
            self.assertEqual(analyze.call_count,1)

    def test_delivery_checkpoint_detects_changes_and_another_command_payload(self):
        from knowledge_v2.store import Conflict
        claim=dict(command_id=str(uuid.uuid4()),kind='source.analyze',payload={'source_id':'source'})
        self.bridge.analysis_delivery(claim,{'summary':{'semantic_completeness':'partial'}})
        with self.assertRaises(Conflict):
            self.bridge.analysis_delivery(dict(claim,payload={'source_id':'other'}))
        path=self.bridge.store.directory/'analysis-deliveries'/(claim['command_id']+'.json')
        saved=json.loads(path.read_text());saved['analysis']['summary']['semantic_completeness']='model_reviewed'
        path.write_text(json.dumps(saved))
        with self.assertRaises(Conflict):self.bridge.analysis_delivery(claim)
