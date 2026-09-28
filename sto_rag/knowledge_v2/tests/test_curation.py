import copy,unittest
from knowledge_v2.curation import diff,trust,context_digest,fingerprint
from knowledge_v2.review import curation_applicability,compound_decisions


class CurationTests(unittest.TestCase):
    def test_curated_split_alternatives_are_evaluated_as_one_requirement(self):
        def decision(identity,state):return dict(state=state,obligation=dict(id=identity,document_id='doc',release_id='release',
            requirement_ref=[identity,1],publication_trust={'policy':'curation'},composition={'atom':[identity]},
            composition_group={'parent':'original','logic':{'any_of':['a','b']}}))
        a,b=decision('a','violated'),decision('b','checked')
        self.assertEqual(compound_decisions([a,b])[0]['state'],'checked');self.assertEqual(a['state'],'unknown')
        a=decision('a','violated')
        self.assertEqual(compound_decisions([a])[0]['state'],'unknown');self.assertEqual(a['state'],'unknown')
        a,b=decision('a','violated'),decision('b','violated')
        self.assertEqual(compound_decisions([a,b])[0]['state'],'violated')
        a,b=decision('a','unknown'),decision('b','unknown');a['preliminary_violation']=True
        compound_decisions([a,b]);self.assertFalse(a['preliminary_violation'])

    def test_curated_single_atom_is_a_valid_compound(self):
        d=dict(state='violated',obligation=dict(id='a',document_id='doc',release_id='release',requirement_ref=['req',1],
            publication_trust={'policy':'curation'},composition={'atom':['action']}))
        self.assertEqual(compound_decisions([d])[0]['state'],'violated')

    def test_confidence_and_expert_mark_cannot_override_hard_blocks(self):
        card=dict(expert_status='confirmed',model_confidence=1,ambiguities=['Unread table'],validation={
            'provenance':{'status':'failed'},'completeness':{'semantic':'unknown'}},dependencies=[{'unresolved':True}])
        result=trust(card,{'context_digest':'context'},'context',False)
        self.assertTrue(result['approval_current']);self.assertEqual(len(result['blocking_reasons']),4)

    def test_semantic_diff_and_renumber_do_not_confuse_matching_address_with_identity(self):
        old=dict(lineage='old',source_family='family',semantic='meaning',locator='3.1',context='context',profiles=['p'],trust={})
        new=dict(old,lineage='new',locator='7.2')
        d=diff([old],[new])[0];self.assertIn('renumbered',d['changes']);self.assertFalse(d['match_requires_review'])
        new=dict(old,lineage='new',semantic='different meaning')
        d=diff([old],[new])[0];self.assertIn('meaning_changed',d['changes']);self.assertTrue(d['match_requires_review'])
        repeated=dict(old,lineage='second');self.assertEqual(len(diff([old,repeated],[new])),3)

    def test_unrelated_profile_facet_does_not_veto_matching_profile(self):
        profiles=[dict(id='a',name='A',inherits=[],version=1,basis=['test'],expression={'fact':{'name':'type','in':['A']}}),
                  dict(id='b',name='B',inherits=[],version=1,basis=['test'],expression={'fact':{'name':'type','in':['B']}})]
        facts={'type':dict(value='A',evidence=[dict(source='s',locator='title')])}
        result=curation_applicability({'profile_ids':['a','b']},profiles,facts,lambda *_:True)
        self.assertEqual(result['result'],'applicable')
