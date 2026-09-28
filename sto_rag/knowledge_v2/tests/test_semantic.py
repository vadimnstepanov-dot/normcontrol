import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from knowledge_v2.semantic import extract_semantic,validate_entity,validate_json,SCHEMA,packets,exact_cite,parameter_grounded,profiles
from knowledge_v2.structural_model import ContextBudgetExceeded,IncompleteInterpretation
from knowledge_v2.store import KnowledgeStore,checksum
from knowledge_v2.norm_runtime import analyze_source

SID=str(uuid.uuid5(uuid.NAMESPACE_URL,'semantic-fixture'))
def block(loc,text,**kw):
    return dict(id=str(uuid.uuid5(uuid.UUID(SID),loc)),locator=loc,exact_text=text,context_hash=checksum(text),
                source_sha256='a'*64,kind='paragraph',**kw)

def entity(b,**kw):
    return dict(description=b['exact_text'],type='requirement',modality='mandatory',
        citations=[dict(locator=b['locator'],quote=b['exact_text'])],conditions=[],exceptions=[],parameters=[],
        composition='atom',parts=[],term='',profile='common',references=[],confidence=.7,uncertainties=[],**kw)

class Model:
    signature='controlled-semantic';calls=0
    def complete(self,policy,data,schema):
        self.calls+=1
        if 'headings' in data:value={'profiles':[]}
        else:
            targets=data['targets'];audit='previous_entities' in data
            value=dict(entities=[] if audit else [entity(b) for b in targets],
                coverage=[dict(locator=b['locator'],disposition='normative',reason='Explicit source') for b in targets])
        return dict(value=value,seconds=.1,usage={'total_tokens':10})

class SemanticTests(unittest.TestCase):
    def test_audit_receives_rejected_parameters_and_keeps_expert_gap(self):
        class Repair(Model):
            def complete(self,policy,data,schema):
                result=super().complete(policy,data,schema)
                b=data['targets'][0]
                if 'previous_entities' in data:
                    assert data['previous_entities']==[]
                    assert data['rejected_entities'][0]['error']=='parameter_not_in_quote'
                    corrected=entity(b)
                    corrected['parameters']=[dict(name='лимит',value='не более семи',unit='',
                        citation=dict(locator='p',quote='не более семи'))]
                    result['value']['entities']=[corrected]
                else:
                    result['value']['entities'][0]['parameters']=[dict(name='лимит',value='999',unit='',
                        citation=dict(locator='p',quote='не более семи'))]
                return result
        with tempfile.TemporaryDirectory() as tmp:
            result=extract_semantic(SID,{'sha256':'a'*64},[block('p','Повторов должно быть не более семи.')],Path(tmp),Repair())
            self.assertEqual(len(result['cards']),1)
            self.assertEqual(result['cards'][0]['parameters'][0]['value'],'не более семи')
            self.assertFalse(result['cards'][0]['expert_approved'])
            self.assertFalse(result['complete'])

    def test_profile_root_reproposal_merges_only_grounded_equivalent_identity(self):
        blocks=[block('p1','Стандарт'),block('h','Общие требования',is_heading=True)]
        candidate=dict(key='common',name='Общие нормы',kind='common',parent='',document_types=[],
                       basis=[dict(locator='h',quote='Общие требования')])
        class Journal:
            def ask(self,*args):return dict(profiles=[candidate,copy.deepcopy(candidate)])
        accepted,errors=profiles(SID,blocks,Journal())
        self.assertEqual(errors,[])
        self.assertEqual([b['locator'] for b in accepted['common']['basis']],['p1','h'])
        self.assertEqual(accepted['common']['expert_status'],'unreviewed')
        candidate['parent']='different'
        self.assertIn('profile_key_conflict',profiles(SID,blocks,Journal())[1])
        candidate['parent']='';candidate['basis'][0]['quote']='Несуществующая норма'
        self.assertIn('quote_mismatch',profiles(SID,blocks,Journal())[1])

    def test_repeated_paths_are_not_copied_but_missing_refs_keep_original_context(self):
        heading=block('h','Нормативная область '*150)
        blocks=[heading]+[block('p'+str(i),'',heading_refs=['h'],heading_path=[heading['exact_text']]) for i in range(20)]
        blocks[-1].update(heading_refs=['absent'],heading_path=['Неутерянный контекст'])
        all_packets=list(packets(blocks))
        for p,group in all_packets:
            self.assertEqual(len({b['locator'] for b in p['targets']+p['context']}),len(p['targets']+p['context']))
            self.assertLess(len(json.dumps(p,ensure_ascii=False)),26000)
        self.assertIn('Неутерянный контекст',json.dumps(all_packets[-1][0],ensure_ascii=False))
        self.assertEqual([b['locator'] for p,g in all_packets for b in g],[b['locator'] for b in blocks])

    def test_audit_context_overflow_splits_atomically_and_keeps_all_coverage(self):
        class Limited(Model):
            measure_context=True
            def complete(self,policy,data,schema):
                if 'previous_entities' in data and len(data['targets'])>1:
                    raise ContextBudgetExceeded('test token budget')
                return super().complete(policy,data,schema)
        with tempfile.TemporaryDirectory() as tmp:
            blocks=[block('p'+str(i),'Обязательство '+str(i)) for i in range(5)]
            result=extract_semantic(SID,{'sha256':'a'*64},blocks,Path(tmp),Limited())
            self.assertTrue(result['complete'])
            self.assertEqual(len(result['cards']),5)
            self.assertEqual([b['locator'] for b in result['coverage']],[b['locator'] for b in blocks])
            self.assertEqual(len(result['metrics']['packet_splits']),4)
            self.assertFalse(result['errors'])

    def test_indivisible_context_is_visible_and_not_claimed_complete(self):
        class Limited(Model):
            measure_context=True
            def complete(self,*args):raise ContextBudgetExceeded('test token budget')
        with tempfile.TemporaryDirectory() as tmp:
            result=extract_semantic(SID,{'sha256':'a'*64},[block('p','Текст')],Path(tmp),Limited())
            self.assertFalse(result['complete'])
            self.assertEqual(result['coverage'][0]['state'],'needs_review')
            self.assertTrue(result['errors'])

    def test_truncated_output_splits_without_losing_atoms_or_coverage(self):
        class Limited(Model):
            truncated=[]
            def complete(self,policy,data,schema):
                if len(data.get('targets',[]))>1:
                    self.truncated.append(tuple(b['locator'] for b in data['targets']))
                    raise IncompleteInterpretation('truncated output')
                return super().complete(policy,data,schema)
        with tempfile.TemporaryDirectory() as tmp:
            blocks=[block('p'+str(i),'Обязанность '+str(i)) for i in range(4)]
            result=extract_semantic(SID,{'sha256':'a'*64},blocks,Path(tmp),Limited())
            self.assertTrue(result['complete']);self.assertEqual(len(result['cards']),4)
            self.assertEqual(len(result['coverage']),4);self.assertFalse(result['errors'])
            self.assertTrue(result['metrics']['packet_splits'])
            self.assertEqual(len(Limited.truncated),len(set(Limited.truncated)))

    def test_indivisible_truncation_remains_a_visible_gap(self):
        class Limited(Model):
            def complete(self,*args):raise IncompleteInterpretation('truncated output')
        with tempfile.TemporaryDirectory() as tmp:
            result=extract_semantic(SID,{'sha256':'a'*64},[block('p','Обязанность')],Path(tmp),Limited())
            self.assertFalse(result['complete']);self.assertEqual(result['coverage'][0]['state'],'needs_review')
            self.assertIn('indivisible_output_budget_exceeded',[e['code'] for e in result['errors']])

    def test_audit_receives_conditions_exceptions_and_modality(self):
        class Auditor(Model):
            def complete(self,policy,data,schema):
                if 'previous_entities' in data:
                    prior=data['previous_entities'][0]
                    assert prior['conditions']==[dict(locator='p',quote='при согласовании')]
                    assert prior['exceptions']==[dict(locator='p',quote='кроме учебного режима')]
                    assert prior['modality']=='mandatory'
                    return super().complete(policy,data,schema)
                response=super().complete(policy,data,schema)
                response['value']['entities'][0]['conditions']=[dict(locator='p',quote='при согласовании')]
                response['value']['entities'][0]['exceptions']=[dict(locator='p',quote='кроме учебного режима')]
                return response
        with tempfile.TemporaryDirectory() as tmp:
            result=extract_semantic(SID,{'sha256':'a'*64},[block('p','Обязательно при согласовании, кроме учебного режима.')],Path(tmp),Auditor())
            self.assertTrue(result['complete'])

    def test_space_alignment_preserves_original_and_numeric_bounds(self):
        b=block('p1','ГОСТ\u00a0123. Не менее 8000 ч.')
        c=exact_cite(dict(locator='p1',quote='ГОСТ 123.'),{'p1':b})
        self.assertEqual(c['quote'],'ГОСТ\u00a0123.')
        self.assertTrue(parameter_grounded('не менее 8000 ч','Наработка — не менее 8000 ч.'))
        self.assertTrue(parameter_grounded('>= 8000','Наработка — не менее 8000 ч.'))
        self.assertFalse(parameter_grounded('<= 8000','Наработка — не менее 8000 ч.'))
        self.assertFalse(parameter_grounded('800','Наработка — не менее 8000 ч.'))

    def test_or_cannot_silently_become_an_atom(self):
        b=block('p1','Подпись электронная либо бумажная.')
        card=validate_entity(entity(b),{'p1':b},{'p1'},{},SID,'run')
        self.assertIn('unknown',card['composition'])
        self.assertIn('alternative_scope_needs_explicit_composition',card['ambiguities'])

    def test_table_row_carries_headers_and_legend_across_packets(self):
        blocks=[block('h','Режим',table='t1',row=1,column=1),block('limit','Лимит, с',table='t1',row=1,column=2),
                block('note','Кроме тестового режима.'),block('r','Рабочий',table='t1',row=2,column=1,header_refs=['h']),
                block('v','7',table='t1',row=2,column=2,header_refs=['limit'],note_refs=['note'])]
        packet=next(p for p,group in packets(blocks,limit=1) if group[0]['locator']=='v')
        self.assertTrue({'r','h','limit','note'}<={b['locator'] for b in packet['context']})

    def test_resume_and_confidence_not_expert(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocks=[block('p1','Состав: назначение и границы.')];model=Model()
            result=extract_semantic(SID,{'sha256':'a'*64},blocks,Path(tmp),model)
            count=model.calls
            resumed=extract_semantic(SID,{'sha256':'a'*64},blocks,Path(tmp),model)
            self.assertEqual(model.calls,count);self.assertEqual(result['cards'],resumed['cards'])
            self.assertEqual(result['coverage_audit']['accounted'],1)
            self.assertEqual(result['cards'][0]['expert_status'],'unreviewed')
            self.assertFalse(result['cards'][0]['expert_approved'])

    def test_false_quote_and_unverified_arrow_are_rejected(self):
        b=block('p1','Пароли запрещены.');e=entity(b)
        e['citations'][0]['quote']='Пароли обязательны.'
        with self.assertRaisesRegex(ValueError,'quote_mismatch'):
            validate_entity(e,{'p1':b},{'p1'},{},SID,'run')
        b['eligible_for_requirement_extraction']=False
        with self.assertRaisesRegex(ValueError,'unverified_visual'):
            validate_entity(entity(b),{'p1':b},{'p1'},{},SID,'run')

    def test_external_reference_is_not_resolved_by_model(self):
        b=block('p1','См. пункт 6.3 внешнего стандарта.');e=entity(b)
        e['references']=[dict(pointer='пункт 6.3',relation='refers_to',locator='p1')]
        card=validate_entity(e,{'p1':b},{'p1'},{},SID,'run')
        self.assertTrue(card['dependencies'][0]['unresolved'])
        self.assertIsNone(card['dependencies'][0]['target'])

    def test_truncated_schema_and_invalid_types(self):
        with self.assertRaises(ValueError):validate_json({'entities':[]},SCHEMA)
        e=entity(block('p1','test'));e['confidence']=True
        with self.assertRaises(ValueError):validate_json({'entities':[e],'coverage':[]},SCHEMA)

    def test_full_material_not_regex_gated(self):
        blocks=[block('p'+str(i),'Без специальных ключевых слов '+str(i)) for i in range(90)]
        seen=[b['locator'] for _,group in packets(blocks,150) for b in group]
        self.assertEqual(seen,[b['locator'] for b in blocks])

    def test_canonical_persistence_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=KnowledgeStore(tmp);store.register_set('set','scope',{'name':'test'})
            b=block('p1','В составе документа — назначение.')
            store.put_record('set','source_revision',SID,1,dict(sha256='a'*64,original_key='original/test.docx',parser_version='fixture'))
            store.put_record('set','fragment',b['id'],1,dict(source_revision=[SID,1],locator='p1',exact_text=b['exact_text'],
                context_hash=b['context_hash'],search_text=b['exact_text'],structure={'kind':'paragraph'}))
            first=analyze_source(store,'set',SID,client=Model());counts=store.counts()
            second=analyze_source(store,'set',SID,client=Model())
            self.assertEqual(counts,store.counts());self.assertEqual(first['run_id'],second['run_id'])
            self.assertTrue(first['summary']['profiles']);self.assertEqual(first['summary']['extractor_version'],'semantic-9.1.3')

    def test_failed_coverage_does_not_claim_complete(self):
        class Missing(Model):
            def complete(self,*args,**kw):
                result=super().complete(*args,**kw);result['value']['coverage']=[];return result
        with tempfile.TemporaryDirectory() as tmp:
            result=extract_semantic(SID,{'sha256':'a'*64},[block('p1','Состав: код.')],Path(tmp),Missing())
            self.assertFalse(result['complete']);self.assertTrue(result['errors'])

if __name__=='__main__':unittest.main()
