import copy,unittest
from knowledge_v2.review_wire import payload,TRACE_REFS_VERSION,TRACE_COMPACT_VERSION
from knowledge_v2.tests.test_gap_transport import GapTransportTests

class TraceWireTests(GapTransportTests):
    def trace_fixture(self):
        p=super().fixture();p['stage']='trace_check'
        c=dict(locator='p14',quote='Сохранить ВСЕ цели, кроме доказанных исключений.',source_sha256='a'*64,context_hash='b'*64,start=0,end=46,unknown={'x':[1,None]})
        p['obligations'][0]['link']=dict(basis_citations=[c,c],normative_basis=[dict(card_id='card',citations=[c])],condition={'fact':{'name':'stage','in':['design']}},trusted=False)
        return p
    def test_shared_citations_preserve_order_duplicates_and_unknown_fields(self):
        p=self.trace_fixture();original=copy.deepcopy(p);v,_=payload(p,version=TRACE_COMPACT_VERSION)
        link=v['obligations'][0]['link'];self.assertEqual(link['basis_citation_refs'],['TC1','TC1'])
        self.assertEqual(link['normative_basis'][0]['citation_refs'],['TC1'])
        c=v['trace_citations']['TC1'];old=original['obligations'][0]['link']['basis_citations'][0]
        for k in ('quote','locator','unknown'):self.assertEqual(c[k],old[k])
        self.assertEqual(v['sources'][c['source_ref']],'a'*64)
        self.assertEqual(link['condition'],original['obligations'][0]['link']['condition'])
        self.assertFalse(link['trusted']);self.assertEqual(p,original)
    def test_v7_representation_is_unchanged(self):
        p=self.trace_fixture();v,_=payload(p,version=TRACE_REFS_VERSION)
        self.assertEqual(v['obligations'][0]['link'],p['obligations'][0]['link']);self.assertNotIn('trace_citations',v)
    def test_v8_uses_exact_block_refs_and_distinct_pin(self):
        p=self.trace_fixture();c=self.client(TRACE_COMPACT_VERSION)
        props=c.request(p)['response_format']['json_schema']['schema']['properties']['decisions']['items']['properties']
        self.assertNotIn('quote',props['evidence']['items']['properties'])
        self.assertNotEqual(c.signature,self.client(TRACE_REFS_VERSION).signature)
    def test_document_names_are_restorable_and_source_blocks_unchanged(self):
        p=self.trace_fixture();p['documents'][0]['document_name']='Полное имя ЧТЗ.docx';before=copy.deepcopy(p)
        v,_=payload(p,version=TRACE_COMPACT_VERSION)
        structure=v['document_structures'][v['documents'][0][2]]
        self.assertEqual(v['document_names'][structure['document_name_ref']],'Полное имя ЧТЗ.docx')
        self.assertEqual(p,before)
