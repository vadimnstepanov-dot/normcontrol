import unittest
from knowledge_v2.document_facts import validate,requested,verified,anchor_evidence,definitions
from knowledge_v2.applicability import evaluate

class FactsTests(unittest.TestCase):
    def setUp(self):
        self.doc={'id':'d','blocks':[{'id':'b','locator':'p1','text':'Заказчик ОАО «РЖД». Функциональное развитие прикладной системы.'}]}
        self.names={'organization':['ОАО РЖД'],'excluded_system_class':['стандартное ПО']}
    def row(self,name,values,complete=True):
        return dict(name=name,values=values,complete=complete,confidence=.95,reason='Explicit object and customer',evidence=[dict(block_id='b',quote=self.doc['blocks'][0]['text'])])
    def test_evidenced_empty_class_collection_allows_not_predicate(self):
        facts=validate(self.doc,self.names,[self.row('organization',['ОАО РЖД']),self.row('excluded_system_class',[])])
        result=evaluate({'not':{'fact':{'name':'excluded_system_class','in':['стандартное ПО']}}},facts,lambda n,v,e:verified(facts,n,v,e))
        self.assertEqual(result['result'],'applicable')
    def test_unknown_collection_never_becomes_negative_proof(self):
        r=self.row('excluded_system_class',[],False)
        facts=validate(self.doc,self.names,[self.row('organization',['ОАО РЖД']),r])
        self.assertNotIn('excluded_system_class',facts)
    def test_forged_quote_rejected(self):
        r=self.row('organization',['ОАО РЖД']);r['evidence'][0]['quote']='not present'
        with self.assertRaises(ValueError):validate(self.doc,self.names,[r])
    def test_low_confidence_not_promoted(self):
        r=self.row('organization',['ОАО РЖД']);r['confidence']=.5
        self.assertEqual(validate(self.doc,{'organization':['ОАО РЖД']},[r]),{})
    def test_profile_predicates_drive_names(self):
        self.assertEqual(requested({('p',1):dict(kind='profile',payload={'fact':{'name':'organization','in':['A']}})}),{'organization':['A']})
    def test_quote_typography_can_be_restored_but_words_cannot(self):
        row=self.row('organization',['ОАО РЖД']);row['evidence']=[dict(block_id='0',quote='Заказчик ОАО "РЖД". Функциональное развитие прикладной системы.')]
        anchor_evidence([row],self.doc,{'0':'b'})
        self.assertEqual(row['evidence'][0]['quote'],self.doc['blocks'][0]['text'])
        row['evidence'][0]['quote']='Заказчик ОАО "иная организация".'
        anchor_evidence([row],self.doc,{})
        with self.assertRaises(ValueError):validate(self.doc,{'organization':['ОАО РЖД']},[row])
    def test_definition_comes_from_source(self):
        records={('f',1):dict(kind='fragment',payload=dict(locator='2',exact_text='стандартное ПО: системное или массовое неспецифическое ПО.'))}
        self.assertEqual(len(definitions(records,self.names)),1)
