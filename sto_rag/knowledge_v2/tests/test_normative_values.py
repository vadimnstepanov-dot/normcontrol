import copy
import unittest
from knowledge_v2.normative_values import pack, unpack, REF
from knowledge_v2.store import Conflict, encode


class NormativeValuesTests(unittest.TestCase):
    def test_nested_values_restore_conditions_exceptions_sources_and_row_identity(self):
        text='Условие: при тиражировании сохранить параметры. Исключение: только согласованные случаи. '*8
        context={'text':text,'source':{'name':'СТО','clause':'4.2','revision':'approved'}}
        source={'obligations':[{'id':'a','atom':context,'exceptions':[text]},
                               {'id':'b','atom':context,'exceptions':[text]}],
                'normative_contexts':{'a':[context],'b':[context]},
                'documents':[{'id':'x','text':text}]}
        before=copy.deepcopy(source); compact=pack(source)
        self.assertEqual(source,before)
        self.assertEqual(unpack(compact),source)
        self.assertEqual([r['id'] for r in compact['obligations']],['a','b'])
        self.assertEqual(compact['documents'],source['documents'])
        self.assertLess(len(encode(compact)),len(encode(source)))

    def test_reserved_reference_is_rejected(self):
        with self.assertRaises(Conflict):pack({'obligations':[{'id':'a','atom':{REF:'V1'}}]})

    def test_missing_and_cyclic_references_fail_closed(self):
        for pool in ({},{'V1':{REF:'V1'}},{'V1':{REF:'V2'},'V2':{REF:'V1'}}):
            with self.subTest(pool=pool),self.assertRaises(Conflict):
                unpack({'obligations':[{'id':'a','atom':{REF:'V1'}}],'normative_values':pool})

    def test_distinct_values_and_short_repeats_are_not_collapsed(self):
        source={'obligations':[{'id':'a','atom':'short'}, {'id':'b','atom':'short'},
                               {'id':'c','atom':'x'*200}, {'id':'d','atom':'y'*200}]}
        self.assertEqual(pack(source),source)

    def test_empty_and_legacy_packets(self):
        for source in ({},{'obligations':[]},{'obligations':[{'id':'a','atom':'legacy'}]}):
            self.assertEqual(unpack(pack(source)),source)
