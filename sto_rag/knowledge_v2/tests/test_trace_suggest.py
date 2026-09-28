import copy,tempfile,unittest,uuid
from pathlib import Path
from knowledge_v2.store import KnowledgeStore,checksum
from knowledge_v2.trace_suggest import suggest

class TraceSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=KnowledgeStore(Path(self.tmp.name))
        self.store.register_set('set','scope',{})
        self.cards=[]
        for quote in ('Требования должны быть раскрыты в задании.','Испытания должны подтверждать выполнение задания.'):
            identity=str(uuid.uuid4());card={'citations':[{'quote':quote}]}
            self.store.put_record('set','requirement',identity,1,dict(card=card,source_revision=['source',1],fragment_refs=[],modality='mandatory',condition={'unknown':'Fixture scope'}))
            self.cards.append(dict(id=identity,base_id=identity,revision=1,digest=checksum(card),payload=card))
        self.payload=dict(set_id='set',actor_id='expert',cards=self.cards,document_types=['ТЗ','ПМИ'])
        link=dict(source=self.cards[0]['id'],target=self.cards[1]['id'],basis=self.cards[1]['id'],relation='verifies',
            source_type='ТЗ',target_type='ПМИ',description='Проверка выполнения задания.',basis_quote=self.cards[1]['payload']['citations'][0]['quote'],
            mandatory_target=False,confidence=.8)
        class Model:
            signature='suggest-test';timeout=30;calls=0
            def complete(m,p):m.calls+=1;return {'links':[copy.deepcopy(link)]}
        self.client=Model();self.link=link
    def test_exact_proposal_is_unconfirmed_and_redelivery_has_no_model_call(self):
        first=suggest(self.store,'one',self.payload,self.client,lambda *a:True)
        again=suggest(self.store,'one',self.payload,self.client,lambda *a:True)
        self.assertEqual(first,again);self.assertFalse(first['expert_validation']);self.assertEqual(self.client.calls,1)
    def test_entity_category_is_not_a_document_role(self):
        self.link['source_type']='requirement'
        with self.assertRaisesRegex(ValueError,'document roles'):suggest(self.store,'bad-role',self.payload,self.client,lambda *a:True)
    def test_forged_normative_basis_is_rejected(self):
        self.link['basis_quote']='ПМИ всегда обязательна.'
        with self.assertRaisesRegex(ValueError,'quote'):suggest(self.store,'forged',self.payload,self.client,lambda *a:True)
    def test_no_document_profiles_no_invented_roles(self):
        self.payload['document_types']=[]
        result=suggest(self.store,'empty',self.payload,self.client,lambda *a:True)
        self.assertEqual(result['suggestions'],[]);self.assertEqual(self.client.calls,0)
