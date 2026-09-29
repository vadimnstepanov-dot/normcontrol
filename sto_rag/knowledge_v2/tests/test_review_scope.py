import unittest
from knowledge_v2.applicability import evaluate, match_profiles
from knowledge_v2.review import scope_findings


def fact(name, values):
    return {'fact':{'name':name,'in':values}}


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.exclude={'not':fact('excluded_system_class',['стандартное ПО'])}
        self.expression={'all_of':[fact('organization',['ОАО РЖД']),self.exclude,
                                  fact('document_type',['ОИТ'])]}
        self.facts={'document_type':{'value':'ОИТ','evidence':[{'source':'doc','locator':'p1','quote':'ОИТ'}]}}

    def evaluate(self, expression=None, verified=True):
        return evaluate(expression or self.expression,self.facts,lambda *args:verified,review_scope=True)

    def test_default_scope_does_not_fabricate_document_facts(self):
        self.assertEqual(self.evaluate()['result'],'applicable')
        self.assertEqual(set(self.facts),{'document_type'})
        self.assertEqual(evaluate(self.expression,self.facts,lambda *args:True)['result'],'unknown')

    def test_wrong_document_type_stays_inapplicable(self):
        self.facts['document_type']['value']='ПМИ'
        self.assertEqual(self.evaluate()['result'],'not_applicable')

    def test_stage_and_local_exceptions_stay_unknown(self):
        self.assertEqual(self.evaluate(fact('stage',['создание']))['result'],'unknown')
        self.assertEqual(self.evaluate({'not':fact('has_appendices',[True])})['result'],'unknown')
        self.assertEqual(self.evaluate(fact('excluded_system_class',['стандартное ПО']))['result'],'unknown')

    def test_verified_exclusion_is_respected_and_deduplicated(self):
        self.facts['excluded_system_class']={'value':'стандартное ПО','evidence':[{'source':'doc','locator':'p2','quote':'стандартное ПО'}]}
        result=self.evaluate()
        self.assertEqual(result['result'],'not_applicable')
        row=dict(document_id='doc',release_id='rel',applicability=result,atom={'action':'проверять'})
        notices=scope_findings([{'obligation':row},{'obligation':row}])
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0]['state'],'unknown')
        self.assertFalse(notices[0]['preliminary_violation'])
        self.assertEqual(notices[0]['evidence'][0]['quote'],'стандартное ПО')

    def test_unverified_exclusion_cannot_block_review(self):
        self.facts['excluded_system_class']={'value':'стандартное ПО','evidence':[{'source':'doc','locator':'p2'}]}
        self.assertEqual(self.evaluate(self.exclude,verified=False)['result'],'applicable')

    def test_source_selection_is_not_overridden(self):
        self.assertEqual(self.evaluate(fact('selected_sources',['source']))['result'],'unknown')

    def test_inherited_profile_uses_same_scope_policy(self):
        profiles=[dict(id='root',name='РЖД',expression=self.expression,basis=[{}],version=1),
                  dict(id='child',name='ОИТ',inherits=['root'],expression=fact('document_type',['ОИТ']),basis=[{}],version=1)]
        self.assertEqual(match_profiles(profiles,self.facts,lambda *a:True,review_scope=True)['child']['result'],'applicable')

    def test_type_mismatch_is_not_exclusion_notice(self):
        self.facts['document_type']['value']='ПМИ'
        row=dict(document_id='doc',release_id='rel',applicability=self.evaluate(),atom={})
        self.assertEqual(scope_findings([{'obligation':row}]),[])

    def test_explicit_expert_exception_has_its_own_notice(self):
        app=dict(result='not_applicable',exception_ref=['exception',2],exception_basis=[{'locator':'4.2'}],
                 evidence=[dict(fact='work_type',value='сопровождение',evidence=[dict(source='doc',locator='p3',quote='сопровождение')])])
        row=dict(document_id='doc',release_id='rel',applicability=app,atom={})
        result=scope_findings([{'obligation':row}])
        self.assertEqual(result[0]['exception_ref'],['exception',2])
        self.assertEqual(result[0]['state'],'unknown')

    def test_nonmatching_exclusion_fact_is_not_a_notice(self):
        self.facts['excluded_system_class']={'value':'иная система','complete':True,
            'evidence':[dict(source='doc',locator='p2',quote='иная система')]}
        self.facts['document_type']['value']='ПМИ'
        row=dict(document_id='doc',release_id='rel',applicability=self.evaluate(),atom={})
        self.assertEqual(scope_findings([{'obligation':row}]),[])
