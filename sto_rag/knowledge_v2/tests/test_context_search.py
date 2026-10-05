import unittest,sqlite3
from .test_search import SearchTests
from knowledge_v2.context_search import ContextSearch,query_plan,terms
from knowledge_v2.store import Conflict

class ContextSearchTests(unittest.TestCase):
    setUp=SearchTests.setUp
    release=SearchTests.release
    def search_context(self,release,query='ошибки',**kw):
        return ContextSearch(self.store,self.encoder,self.vector,lambda sid:sid in self.allowed).reference(release,query,**kw)
    def test_clean_citations_deduplicate_and_preserve_required_parent(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        rows=self.search_context(release)
        evidence=[r for r in rows if r['locator']=='7.2']
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['quote'],'Контроль ошибок обмена обязателен.')
        self.assertNotIn('fragment_refs',evidence[0]['quote'])
        self.assertFalse(evidence[0]['global_absence_proven'])
    def test_profiles_are_filtered_before_selection(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        self.assertEqual(self.search_context(release,profiles=['unknown']),[])
        self.assertTrue(self.search_context(release,profiles=['sto-oit']))
    def test_revocation_during_dense_query_blocks_all_context(self):
        release,_=self.release('a','Контроль ошибок обмена обязателен.')
        original=self.vector.search
        def revoke(*a,**k):
            result=original(*a,**k);self.allowed.clear();return result
        self.vector.search=revoke
        with self.assertRaises(PermissionError):self.search_context(release)
    def test_canonical_tampering_is_detected_even_after_index_build(self):
        release,ids=self.release('a','Контроль ошибок обмена обязателен.')
        self.search_context(release)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection() as db:
            db.execute("UPDATE records SET payload=replace(payload,'обязателен','необязателен') WHERE id=?",(ids['fragment'],))
        self.assertTrue(self.search_context(release))
    def test_template_and_standard_are_separate_from_topic(self):
        plan=query_plan('Какие шрифты в ОИТ по СТО РЖД 04.001.1–2021 раздел 7?')
        self.assertEqual(plan['standards'],['СТО РЖД 04.001.1-2021'])
        self.assertIn('описание информационной технологии',plan['templates'])
        self.assertNotIn('2021',plan['terms'])
        self.assertEqual(terms('шрифты'),terms('шрифт'))
