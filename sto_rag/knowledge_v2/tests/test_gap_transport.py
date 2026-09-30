import copy,json,unittest
from unittest.mock import patch
from knowledge_v2.review_client import LlamaClient
from knowledge_v2.review_wire import payload,VERSION,GROUPED_VERSION
from knowledge_v2.gap_wire import unpack,POLICY
from knowledge_v2.store import Conflict

class GapTransportTests(unittest.TestCase):
    def fixture(self):
        return dict(stage='check',obligations=[dict(id='rule')],documents=[dict(id='block',text='Условие.')],
            completeness=dict(full_text=False,gaps=[dict(locator='p'+str(i),state='unread',reason='Нумерация',extra=None) for i in range(1,101)]))
    def client(self,version):
        def http(c,path,*a,**kw):
            if path=='/props':return {'default_generation_settings':{'n_ctx':49152}}
            if path=='/v1/models':return {'data':[{'id':'test'}]}
            if path=='/v1/chat/completions':return {'choices':[dict(finish_reason='stop',message={'content':json.dumps({'decisions':[
                dict(obligation_id='R001',outcome='unknown',claim='unknown',reason='Недостаточно данных',evidence=[{'block_id':'B001'}])
            ]})})]}
            raise AssertionError(path)
        with patch.object(LlamaClient,'http',http),patch('knowledge_v2.model_profile.enabled',return_value=False):
            c=LlamaClient('http://localhost',wire_version=version)
        c.http=lambda *a,**kw:http(c,*a,**kw)
        c.count=lambda p:0
        return c
    def test_explicit_version_restores_every_gap_and_preserves_canonical_input(self):
        original=self.fixture();before=copy.deepcopy(original)
        old,_=payload(original);new,_=payload(original,version=GROUPED_VERSION)
        self.assertEqual(old['completeness']['gaps'],unpack(new['completeness']['gaps']))
        self.assertEqual(old['completeness']['gap_reasons'],new['completeness']['gap_reasons'])
        self.assertFalse(new['completeness']['full_text']);self.assertEqual(original,before)
        self.assertEqual(old['transport'],VERSION);self.assertEqual(new['transport'],GROUPED_VERSION)
    def test_old_default_request_and_signature_stay_compatible(self):
        c=self.client(VERSION);expected=c.request(self.fixture());signature=c.signature
        del c.wire_version
        self.assertEqual(expected,c.request(self.fixture()))
        newer=self.client(GROUPED_VERSION)
        self.assertNotEqual(signature,newer.signature)
        self.assertNotIn(POLICY,expected['messages'][0]['content'])
        self.assertIn(POLICY,newer.request(self.fixture())['messages'][0]['content'])
    def test_complete_uses_pinned_transport_and_restores_evidence(self):
        c=self.client(GROUPED_VERSION)
        with patch('knowledge_v2.model_profile.ensure'):
            d=c.complete(self.fixture())['decisions'][0]
            self.assertEqual(d['obligation_id'],'rule')
            self.assertEqual(d['evidence'],[{'block_id':'block','quote':'Условие.'}])
            c.wire_version=VERSION
            with self.assertRaises(Conflict):c.complete(self.fixture())
    def test_unknown_wire_rejected_before_network(self):
        with patch.object(LlamaClient,'http',side_effect=AssertionError('network')):
            with self.assertRaises(ValueError):LlamaClient('http://localhost',wire_version='future')
        with self.assertRaises(ValueError):payload({},version='future')
