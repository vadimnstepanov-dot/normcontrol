import copy,json,tempfile,unittest
from pathlib import Path
from .test_semantic import block,entity,SID,Model
from knowledge_v2.condition_transfer import ConditionTransfer,validate_matrix
from knowledge_v2.semantic import extract_semantic,validate_entity
from knowledge_v2.norm_runtime import analyze_source
from knowledge_v2.store import KnowledgeStore,checksum


def material():
    note=block('n','«A» — вариант по выбору. Для таких вариантов требуется согласование владельца. '
        'Решение фиксируют в протоколе. «B» — обязательный вариант, кроме учебной среды.')
    # Addresses intentionally differ from any reference document.
    a=block('x','A',table='matrix',row=2,column=1,note_refs=['n'])
    b=block('y','B',table='matrix',row=3,column=1,note_refs=['n'])
    a['kind']=b['kind']='table_cell'
    return [note,a,b]


class Decisions:
    def __init__(self,drop=False,disagree=False):self.calls=0;self.drop=drop;self.disagree=disagree
    def ask(self,phase,policy,data,schema):
        self.calls+=1;rows=[]
        for s in data['subjects']:
            for u in data['units']:
                role='not_applicable'
                if s['value']=='A':role={'u0':'context','u1':'condition','u2':'required_action'}.get(u['id'],role)
                if s['value']=='B' and u['id']=='u3':role='exception'
                if self.disagree and phase.startswith('conditions_audit') and u['id']=='u1':role='not_applicable'
                rows.append(dict(subject=s['key'],unit=u['id'],role=role,reason='Source clause relation'))
        return dict(decisions=rows[:-1] if self.drop else rows)


class TransferTests(unittest.TestCase):
    def test_complete_agreement_and_recording_transferred_without_other_category(self):
        blocks=material();journal=Decisions();resolver=ConditionTransfer(blocks,journal)
        raw=entity(blocks[1]);raw.update(type='permission',modality='permitted')
        value,review=resolver.resolve(raw,{'x'})
        self.assertEqual([c['quote'] for c in value['conditions']],
                         ['Для таких вариантов требуется согласование владельца.','Решение фиксируют в протоколе.'])
        self.assertFalse(value['exceptions']);self.assertEqual(review['status'],'model_reviewed')
        other,review_b=resolver.resolve(entity(blocks[2]),{'y'})
        self.assertFalse(other['conditions'])
        self.assertEqual(other['exceptions'][0]['quote'],'«B» — обязательный вариант, кроме учебной среды.')
        self.assertEqual(journal.calls,2)  # Shared two-pass matrix, not two new calls per row.
        self.assertFalse(review_b['expert_approved'])

    def test_missing_decision_or_conflicting_scope_cannot_claim_complete(self):
        for journal in (Decisions(drop=True),Decisions(disagree=True)):
            blocks=material();value,review=ConditionTransfer(blocks,journal).resolve(entity(blocks[1]),{'x'})
            self.assertEqual(review['status'],'needs_review');self.assertTrue(value['uncertainties'])
            self.assertNotIn('Для таких вариантов требуется согласование владельца.',[c['quote'] for c in value['conditions']])

    def test_footnote_binding_never_expands_to_other_numbered_notes(self):
        b=block('x','Выбор',table='t',row=1,column=1,note_refs=['n'],
                note_bindings=[dict(source='n',exact_text='Разрешено после согласования.')])
        n=block('n','Разрешено после согласования. Другой документ запрещён.')
        class Scoped:
            def ask(self,phase,policy,data,schema):
                assert len(data['units'])==1
                assert data['units'][0]['text']=='Разрешено после согласования.'
                return dict(decisions=[dict(subject='target',unit='u0',role='condition',reason='Bound note')])
        value,review=ConditionTransfer([b,n],Scoped()).resolve(entity(b),{'x'})
        self.assertEqual(review['status'],'model_reviewed')
        self.assertEqual(value['conditions'],[dict(locator='n',quote='Разрешено после согласования.')])

    def test_false_note_binding_and_visual_hypothesis_not_accepted(self):
        for kwargs in ({'eligible_for_requirement_extraction':False},{}):
            blocks=material();blocks[0].update(kwargs)
            if not kwargs:blocks[1]['note_bindings']=[dict(source='n',exact_text='Не существующая цитата')]
            raw,review=ConditionTransfer(blocks,Decisions()).resolve(entity(blocks[1]),{'x'})
            self.assertEqual(review['status'],'needs_review');self.assertFalse(raw['conditions'])

    def test_different_legends_do_not_share_decisions(self):
        blocks=material();second=[copy.deepcopy(b) for b in blocks]
        for b in second:
            b['locator']+='2'
            if b.get('note_refs'):b['note_refs']=['n2']
        second[0]['exact_text']=second[0]['exact_text'].replace('владельца','комиссии')
        journal=Decisions();resolver=ConditionTransfer(blocks+second,journal)
        resolver.resolve(entity(blocks[1]),{'x'});value,_=resolver.resolve(entity(second[1]),{'x2'})
        self.assertEqual(journal.calls,4)
        self.assertIn('комиссии',value['conditions'][0]['quote'])

    def test_final_fields_survive_store_projection_and_resume(self):
        class Pipeline(Model):
            def __init__(self):self.calls=0;self.decisions=Decisions()
            def complete(self,policy,data,schema):
                self.calls+=1
                if 'units' in data:
                    value=self.decisions.ask('conditions_audit' if 'независимая' in policy else 'conditions',policy,data,schema)
                elif 'headings' in data:value={'profiles':[]}
                else:
                    raw=entity(next(b for b in data['targets'] if b['locator']=='x'));raw.update(type='permission',modality='permitted')
                    value=dict(entities=[] if 'previous_entities' in data else [raw],coverage=[
                        dict(locator=b['locator'],disposition='normative' if b['locator']=='x' else 'context',reason='Fixture source') for b in data['targets']])
                    if 'previous_entities' in data:assert len(data['previous_entities'][0]['conditions'])==2
                return dict(value=value,seconds=.01,usage={})
        with tempfile.TemporaryDirectory() as tmp:
            store=KnowledgeStore(tmp);store.register_set('set','scope',{'name':'control'})
            store.put_record('set','source_revision',SID,1,dict(sha256='a'*64,original_key='source.docx',parser_version='fixture'))
            for b in material():
                structure={k:v for k,v in b.items() if k not in ('id','locator','exact_text','context_hash','source_sha256')}
                store.put_record('set','fragment',b['id'],1,dict(source_revision=[SID,1],locator=b['locator'],
                    exact_text=b['exact_text'],search_text=b['exact_text'],context_hash=b['context_hash'],structure=structure))
            model=Pipeline();result=analyze_source(store,'set',SID,client=model)
            self.assertTrue(result['complete'])
            card=result['projection'][0]
            self.assertEqual(len(card['conditions']),2);self.assertEqual(card['condition_review']['status'],'model_reviewed')
            for c in card['conditions']:self.assertEqual(c['text'],c['citation']['quote'])
            count=model.calls;again=analyze_source(store,'set',SID,client=model)
            self.assertEqual(again['cards'],result['cards']);self.assertEqual(model.calls,count)
            self.assertFalse(card['expert_approved'])

    def test_duplicate_or_unknown_unit_rejected(self):
        subject=[dict(key='s')];source=[dict(id='u')]
        row=dict(subject='s',unit='u',role='context',reason='source')
        for rows in ([row,row],[dict(row,unit='absent')],[]):
            with self.assertRaises(ValueError):validate_matrix(dict(decisions=rows),subject,source)

    def test_mixed_categories_cannot_be_flattened_into_one_condition_scope(self):
        blocks=material();raw=entity(blocks[1]);raw['citations'].append(dict(locator='y',quote='B'))
        value,review=ConditionTransfer(blocks,Decisions()).resolve(raw,{'x','y'})
        self.assertEqual(review['status'],'needs_review')
        self.assertFalse(value['conditions']);self.assertFalse(value['exceptions'])

    def test_waiver_cannot_silently_narrow_an_already_optional_requirement(self):
        b=block('x','Раздел по выбору',note_refs=['n']);n=block('n','При учебной эксплуатации раздел не требуется.')
        class Waiver:
            def ask(self,*args):return dict(decisions=[dict(subject='target',unit='u0',role='exception',reason='Model waiver')])
        raw=entity(b);raw.update(type='permission',modality='permitted')
        value,review=ConditionTransfer([b,n],Waiver()).resolve(raw,{'x'})
        self.assertFalse(value['exceptions']);self.assertEqual(review['status'],'needs_review')
        self.assertIn('exception_target_unresolved',value['uncertainties'][0])
        raw.update(type='requirement',modality='mandatory')
        value,review=ConditionTransfer([b,n],Waiver()).resolve(raw,{'x'})
        self.assertEqual(value['exceptions'][0]['quote'],n['exact_text']);self.assertEqual(review['status'],'model_reviewed')

if __name__=='__main__':unittest.main()
