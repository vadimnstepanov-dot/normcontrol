import copy,json,unittest
from unittest.mock import patch
from knowledge_v2.tests.test_gap_transport import GapTransportTests
from knowledge_v2.review_wire import VERSION,GROUPED_VERSION,TRACE_REFS_VERSION,payload
from knowledge_v2.gap_wire import unpack

class TraceRefsTests(GapTransportTests):
    def trace(self,stage='trace_collect'):
        p=self.fixture();p['stage']=stage;return p
    def test_v7_ids_are_closed_and_collector_has_no_verdict(self):
        c=self.client(TRACE_REFS_VERSION);p=self.trace();before=copy.deepcopy(p)
        req=c.request(p);fields=req['response_format']['json_schema']['schema']['properties']['decisions']['items']['properties']
        self.assertEqual(fields['evidence']['items']['properties'],{'block_id':{'type':'string','enum':['B001']}})
        self.assertEqual(fields['outcome']['enum'],['unknown']);self.assertEqual(p,before)
        with patch('knowledge_v2.model_profile.ensure'):
            result=c.complete(p)['decisions'][0]
        self.assertEqual(result['evidence'],[{'block_id':'block','quote':'Условие.'}])
    def test_v5_v6_trace_still_has_quotes_and_distinct_signature(self):
        for version in (VERSION,GROUPED_VERSION):
            c=self.client(version);props=c.request(self.trace())['response_format']['json_schema']['schema']['properties']['decisions']['items']['properties']
            self.assertIn('quote',props['evidence']['items']['properties'])
            self.assertNotEqual(c.signature,self.client(TRACE_REFS_VERSION).signature)
    def test_verifier_uses_ids_but_canonical_quotes_are_untouched(self):
        p=self.trace('trace_verify');p['proposed']=[dict(obligation_id='rule',outcome='unknown',claim='unknown',reason='x',evidence=[dict(block_id='block',quote='Условие.')])]
        before=copy.deepcopy(p);req=self.client(TRACE_REFS_VERSION).request(p)
        packed=json.loads(req['messages'][1]['content'])
        self.assertEqual(p,before);self.assertNotIn('quote',packed['proposed'][0]['evidence'][0])
    def test_gap_encoding_remains_lossless(self):
        old,_=payload(self.trace(),version=GROUPED_VERSION);new,_=payload(self.trace(),version=TRACE_REFS_VERSION)
        self.assertEqual(unpack(old['completeness']['gaps']),unpack(new['completeness']['gaps']))
    def test_empty_evidence_scope_has_valid_schema_and_no_permitted_items(self):
        p=self.trace('trace_check');p['documents']=[]
        schema=self.client(TRACE_REFS_VERSION).request(p)['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['decisions']['items']['properties']['evidence']['maxItems'],0)
        self.assertNotIn('"enum": []',json.dumps(schema))
