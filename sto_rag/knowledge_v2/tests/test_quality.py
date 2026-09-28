import copy,json,tempfile,unittest
from pathlib import Path
from knowledge_v2.quality import assessment,duplicate_key,unknown,example_scopes
from knowledge_v2.quality_audit import audit,load
from knowledge_v2.store import KnowledgeStore,NotReady,checksum


def card():
    return dict(id='r',description='Документ должен содержать журнал ошибок.',entity_type='requirement',
        modality='mandatory',model_confidence=.95,locator='p2',profile_id='type',
        citations=[dict(locator='p2',quote='Документ должен содержать журнал ошибок.')],
        conditions=[],exceptions=[],dependencies=[],ambiguities=[],
        composition={'atom':[]},obligations=[{'action':'содержать журнал ошибок'}],
        applicability={'fact':{'name':'document_type','in':['ОИТ']}},
        validation={'provenance':{'status':'verified'},'completeness':{'semantic':'model_reviewed','structural':'complete'}})


class QualityTests(unittest.TestCase):
    def test_example_extends_beyond_neighbour_window_but_not_next_section(self):
        rows=[dict(locator='p'+str(i),exact_text=text,structure=dict(document_position=i,is_heading=heading))
              for i,(text,heading) in enumerate([('Раздел',True),('Пример',False),('Первый текст',False),
                ('Таблица А.1',False),('Третий текст',False),('Должен поддерживать API',False),
                ('Следующий раздел',True),('Должен содержать перечень',False)])]
        result=example_scopes(list(reversed(rows)))
        self.assertEqual(result['p5'],'p1');self.assertNotIn('p7',result)
        self.assertNotIn('p0',result);self.assertNotIn('p6',result)

    def test_uncertain_cards_are_candidates_not_deleted_or_promoted_by_confidence(self):
        c=card();self.assertEqual(assessment(c)['status'],'ready')
        for patch in [dict(ambiguities=['lost condition']),dict(dependencies=[{'unresolved':True}]),
                      dict(applicability={'unknown':'stage'}),dict(composition={'any_of':[]}),dict(modality='unknown')]:
            changed=dict(c,**patch,expert_approved=True,expert_status='confirmed')
            self.assertEqual(assessment(changed)['status'],'candidate')
        self.assertEqual(assessment(c,['p2'])['status'],'candidate')
        self.assertEqual(assessment(c,['p2'],approved=True)['status'],'ready')
        self.assertEqual(assessment(c,['other'])['status'],'ready')

    def test_reference_is_not_mandatory_and_ambiguous_reference_stays_candidate(self):
        for typ in ('definition','recommendation','permission','assumption'):
            c=dict(card(),entity_type=typ,modality='permitted')
            self.assertEqual(assessment(c)['status'],'reference')
            c['ambiguities']=['unclear'];self.assertEqual(assessment(c)['status'],'candidate')

    def test_duplicate_requires_same_scope_numbers_and_evidence(self):
        a=card();self.assertEqual(duplicate_key(a,'s'),duplicate_key(copy.deepcopy(a),'s'))
        for patch in [dict(parameters=[{'value':24}]),dict(profile_id='other'),dict(modality='prohibited'),
                      dict(conditions=[{'text':'при наличии ошибки'}]),dict(exceptions=[{'text':'кроме архива'}])]:
            self.assertNotEqual(duplicate_key(a,'s'),duplicate_key(dict(a,**patch),'s'))
        self.assertNotEqual(duplicate_key(a,'s'),duplicate_key(a,'new-edition'))

    def test_nested_unknown_is_never_assumed_applicable(self):
        self.assertTrue(unknown({'all_of':[card()['applicability'],{'any_of':[{'unknown':'stage'}]}]}))
        self.assertTrue(unknown({}));self.assertFalse(unknown(card()['applicability']))

    def test_context_dependency_requires_pinned_evidence_even_after_expert_approval(self):
        c=card();c['dependencies']=[dict(required=True,unresolved=False,target='p1')]
        self.assertIn('dependency_context',assessment(c,approved=True)['reasons'])
        c['citations'].append(dict(locator='p1',quote='Условие из родительского пункта.'))
        self.assertEqual(assessment(c)['status'],'ready')

    def test_introduction_and_unbound_conditional_fragment_are_not_executable(self):
        c=card()
        for quote,reason in [
            ('При этом журнал ведётся отдельно.','implicit_context'),
            ('Требования приведены в шаблоне документа.','context_statement'),
            ('Утвердить план введения стандарта.','administrative_action')]:
            c['citations'][0]['quote']=quote
            self.assertIn(reason,assessment(c)['reasons'])
        c['citations'][0]['quote']='При этом журнал ведётся отдельно.'
        c['conditions']=[dict(text='При ведении нескольких журналов.')]
        self.assertNotIn('implicit_context',assessment(c)['reasons'])

    def test_context_audit_preserves_neighbours_and_cannot_invent_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            store=KnowledgeStore(folder);store.register_set('s','scope',{})
            store.put_record('s','source_revision','source',1,dict(sha256='a'*64,original_key='source',parser_version='test'))
            for loc,text in [('p1','Только для промышленной эксплуатации.'),('p2',card()['citations'][0]['quote'])]:
                store.put_record('s','fragment',loc,1,dict(source_revision=['source',1],locator=loc,exact_text=text,search_text=text,context_hash=checksum(text)))
            a=dict(run_id='a'*64,source_id='source',cards=[card()],coverage_audit={'gaps':[]})
            class Client:
                signature='test';calls=0
                def complete(self,policy,data,schema):
                    self.calls+=1
                    self.seen=data
                    return dict(value={'decisions':[dict(id='r',status='ready',reason='test',evidence=[{'locator':'p2','quote':'invented'}])]},seconds=0,usage={})
            client=Client();result=audit(store,a,client)
            self.assertIn('p1',[x['locator'] for x in client.seen['context']])
            self.assertEqual(result['decisions']['r']['status'],'candidate')
            self.assertFalse(result['expert_approved']);audit(store,a,client);self.assertEqual(client.calls,1)
            a['cards'][0]['description']='changed'
            with self.assertRaises(NotReady):load(store,a)
