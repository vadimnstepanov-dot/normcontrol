import copy
import unittest
from knowledge_v2.expert import review_import_completeness,manual_basis


class ImportCompletenessReviewTests(unittest.TestCase):
    def card(self):
        return dict(citations=[{'quote':'source'}],conditions=['conditional'],exceptions=['exception'],
                    validation={'provenance':{'status':'verified'},'completeness':{
                        'semantic':'needs_review','structural':'source_quotes_verified',
                        'reasons':['independent_json_requires_expert_review']}})

    def request(self):
        return dict(review_import_completeness=True,resolution_reason='Source obligations and their conditions explicitly reviewed.')

    def test_explicit_import_review_preserves_semantics_and_records_actor(self):
        card=self.card();before=copy.deepcopy(card)
        review_import_completeness(card,self.request(),42)
        self.assertEqual(card['validation']['completeness']['semantic'],'verified')
        self.assertEqual(card['validation']['completeness']['expert_review']['actor_id'],42)
        for key in ['citations','conditions','exceptions']:self.assertEqual(card[key],before[key])

    def test_normal_confirmation_does_not_implicitly_clear_completeness(self):
        card=self.card();before=copy.deepcopy(card)
        review_import_completeness(card,{'resolution_reason':'An ambiguity was reviewed separately.'},42)
        self.assertEqual(card,before)

    def test_cannot_clear_unreadable_or_missing_provenance_or_dependency(self):
        for change in ['unreadable','provenance','dependency','reason','ack']:
            with self.subTest(change=change):
                card=self.card();request=self.request()
                if change=='unreadable':card['validation']['completeness']['reasons'].append('unreadable_table')
                if change=='provenance':card['validation']['provenance']['status']='failed'
                if change=='dependency':card['dependencies']=[{'unresolved':True}]
                if change=='reason':request['resolution_reason']=''
                if change=='ack':request['review_import_completeness']='yes'
                with self.assertRaises(ValueError):review_import_completeness(card,request,42)

    def test_manual_creation_selects_one_proven_source_atom_without_changing_parent(self):
        a,b={'locator':'a'},{'locator':'b'}
        base={'entity_type':'requirement','citations':[a,b],
              'obligations':[{'text':'first','citations':[a]},{'text':'second','citations':[b]}]}
        selected=manual_basis(base,1)
        self.assertEqual(selected['obligations'][0]['text'],'second')
        self.assertEqual(selected['citations'],[b]);self.assertEqual(len(base['obligations']),2)
        with self.assertRaises(ValueError):manual_basis(base)
