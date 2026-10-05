import os,unittest
from unittest.mock import patch
from . import test_search as fixtures
from knowledge_v2.dialogue_search import reference
from knowledge_v2.bridge import Bridge

class DialogueSearchTests(unittest.TestCase):
    setUp=fixtures.SearchTests.setUp
    release=fixtures.SearchTests.release
    def retrieve(self,release,**values):
        payload=dict(set_id='a',release_id=release,query='ошибки',limit=6,dialogue_version='dialogue-rag-v1');payload.update(values)
        return reference(self.store,self.encoder,self.vector,lambda sid:sid in self.allowed,
            payload)
    def test_original_fragment_source_and_canonical_quote_are_preserved(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        rows=self.retrieve(release);fragment=next(x for x in rows if x['record_id']==ids['fragment'])
        self.assertEqual(fragment['source_id'],ids['source']);self.assertEqual(fragment['quote'],'Контроль ошибок обмена обязателен.')
        self.assertEqual(fragment['material_type'],'source_excerpt');self.assertFalse(fragment['global_absence_proven'])
    def test_grant_revoked_during_vector_query_returns_no_quotes(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.');original=self.vector.search
        def revoke(*args,**kwargs):
            found=original(*args,**kwargs);self.allowed.clear();return found
        self.vector.search=revoke
        with self.assertRaises(PermissionError):self.retrieve(release)
    def test_search_limit_is_bounded(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        with self.assertRaises(ValueError):self.retrieve(release,limit=100)
    def test_card_without_single_locator_uses_only_verified_citation_addresses(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        rows=self.search.reference(release,'ошибки',kinds=('requirement',))
        rows[0]['locator']=''
        with patch('knowledge_v2.dialogue_search.HybridSearch.reference',return_value=rows):
            self.assertEqual(self.retrieve(release)[0]['locator'],'7.2')
        rows[0]['locator']=''
        rows[0]['context']=[dict(locator='7.2',exact_text='Другой текст: ошибочное сопоставление.')]
        with patch('knowledge_v2.dialogue_search.HybridSearch.reference',return_value=rows):
            self.assertEqual(self.retrieve(release)[0]['locator'],'')
    def test_search_worker_claims_only_retrieval_not_review_or_editing(self):
        requests=[]
        def transport(path,value):requests.append((path,value));return {'command':None}
        bridge=Bridge(self.store,'https://portal.example/api/v2','x'*32,transport=transport)
        with patch.dict(os.environ,{'KNOWLEDGE_SEARCH_ONLY':'1'}):self.assertFalse(bridge.once())
        self.assertEqual(requests[-1][1]['capabilities'],['normative.search'])
        self.assertIn('dialogue-rag-v1',requests[-1][1]['features'])
