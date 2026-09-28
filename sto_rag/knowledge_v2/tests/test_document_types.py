import unittest,tempfile
from pathlib import Path
from knowledge_v2.store import KnowledgeStore
from knowledge_v2.document_types import classify

class Model:
    signature='test-title'
    def __init__(self):self.calls=0;self.forge=False
    def complete(self,p):
        self.calls+=1;d=p['documents'][0];b=d['blocks'][0]
        return dict(documents=[dict(document_id=d['document_id'],type='ЧТЗ',types=['ЧТЗ','Частное техническое задание'] if 'Частное техническое задание' in p['allowed_types'] else ['ЧТЗ'],confidence=.95,
            evidence=[dict(block_id=b['id'],quote='Чужой заголовок' if self.forge else b['text'])],stage='design',
            stage_evidence=[dict(block_id='stage',quote='Стадия: проектирование.')])])

class DocumentTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=KnowledgeStore(Path(self.tmp.name));self.model=Model()
        self.docs=[dict(id='doc',blocks=[dict(id='title',text='Частное техническое задание',locator='p1'),dict(id='stage',text='Стадия: проектирование.',locator='p2')])]
    def test_expanded_title_and_stage_are_cached_with_exact_source(self):
        classify(self.store,self.docs,['ЧТЗ','ТЗ'],self.model,['design','acceptance'])
        self.assertEqual(self.docs[0]['classification']['type'],'ЧТЗ');self.assertEqual(self.docs[0]['classification']['stage'],'design')
        classify(self.store,self.docs,['ЧТЗ','ТЗ'],self.model,['design','acceptance']);self.assertEqual(self.model.calls,1)
    def test_forged_title_is_not_a_fact(self):
        self.model.forge=True
        with self.assertRaises(ValueError):classify(self.store,self.docs,['ЧТЗ'],self.model)

    def test_full_name_and_abbreviation_are_retained_as_equivalent_facets(self):
        classify(self.store,self.docs,['ЧТЗ','Частное техническое задание'],self.model)
        self.assertEqual(set(self.docs[0]['classification']['types']),{'ЧТЗ','Частное техническое задание'})

    def test_title_in_table_is_sent_and_reference_cannot_override_it(self):
        self.docs[0]['blocks'][0]['table']='t1'
        classify(self.store,self.docs,['ЧТЗ','Частное техническое задание'],self.model)
        self.assertEqual(self.docs[0]['classification']['type'],'ЧТЗ')
        self.docs=[dict(id='other',blocks=[dict(id='reference',text='Разработано на основании частного технического задания.'),
            dict(id='title',text='Описание информационной технологии',table='t1'),
            dict(id='stage',text='Стадия: проектирование.')])]
        classify(self.store,self.docs,['ЧТЗ','Описание информационной технологии'],self.model)
        self.assertEqual(self.docs[0]['classification']['type'],'unknown')
