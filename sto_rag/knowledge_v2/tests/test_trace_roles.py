import copy,unittest
from knowledge_v2.trace_roles import enrich,VERSION,WORKS
from knowledge_v2.trace import plan
from knowledge_v2.tests.test_trace import fixtures,Model

class TraceRoleTests(unittest.TestCase):
    def setup(self):
        docs,facts,link=fixtures()
        docs[0]['blocks'][:0]=[dict(id='title',document='source-doc',locator='p1',text='Т Е Х Н И Ч Е С К О Е   З А Д А Н И Е'),
            dict(id='subtitle',document='source-doc',locator='p3',text='на выполнение Работ по теме:')]
        link['source_type']=WORKS
        link['condition']={'not':{'fact':{'name':'excluded_system_class','in':['excluded']}}}
        return docs,facts,link
    def test_exact_multiblock_role_and_review_scope_route_without_mutation(self):
        docs,facts,link=self.setup();before=copy.deepcopy((docs,facts,link))
        old,initial=plan([link],docs,facts,lambda *a:True,Model())
        self.assertFalse(old);self.assertEqual(initial[0]['state'],'unknown')
        batches,initial=plan([link],docs,facts,lambda *a:True,Model(),routing_policy=VERSION)
        self.assertFalse(initial);self.assertEqual(len(batches),1)
        row=batches[0]['payload']['obligations'][0]
        self.assertEqual(row['source_documents'],['source-doc'])
        self.assertEqual(len(row['routing']['derived_roles']['source-doc']['evidence']),2)
        self.assertFalse(row['routing']['derived_roles']['source-doc']['fact']['complete'])
        self.assertEqual((docs,facts,link),before)
    def test_filename_body_quote_or_unverified_classification_cannot_supply_role(self):
        for mode in ('filename','body','unverified','far_subtitle'):
            docs,facts,link=self.setup()
            if mode=='filename':docs[0]['name']='ТЗ на выполнение работ';docs[0]['blocks']=docs[0]['blocks'][2:]
            if mode=='body':docs[0]['blocks'][0]['text']='В соответствии с техническим заданием'
            if mode=='far_subtitle':docs[0]['blocks'][1:1]=[dict(id='x'+str(i),text='Другое',locator='q'+str(i)) for i in range(4)]
            _,_,_,derived=enrich(docs,facts,lambda *a:mode!='unverified')
            self.assertFalse(derived,mode)
    def test_local_unknown_and_proved_exclusion_are_not_relaxed(self):
        docs,facts,link=self.setup()
        link['condition']={'all_of':[link['condition'],{'fact':{'name':'stage','in':['acceptance']}}]}
        batches,initial=plan([link],docs,facts,lambda *a:True,Model(),routing_policy=VERSION)
        self.assertFalse(batches);self.assertIn('stage',initial[0]['reason'])
        facts['source-doc']['excluded_system_class']=dict(value='excluded',evidence=[dict(source='source-doc',locator='p2')])
        _,initial=plan([link],docs,facts,lambda *a:True,Model(),routing_policy=VERSION)
        self.assertEqual(initial[0]['state'],'not_applicable')
    def test_same_document_alias_is_not_cross_document_proof(self):
        docs,facts,link=self.setup();link['target_type']='ТЗ'
        batches,initial=plan([link],docs,facts,lambda *a:True,Model(),routing_policy=VERSION)
        self.assertFalse(batches);self.assertEqual(initial[0]['state'],'unknown')
        self.assertIn('одном документе',initial[0]['reason'])
    def test_pmi_missing_remains_unknown_for_candidate_link(self):
        docs,facts,link=self.setup();link.update(target_type='ПМИ',trusted=False,mandatory_target=False)
        batches,initial=plan([link],docs,facts,lambda *a:True,Model(),routing_policy=VERSION)
        self.assertFalse(batches);self.assertEqual(initial[0]['state'],'unknown')
