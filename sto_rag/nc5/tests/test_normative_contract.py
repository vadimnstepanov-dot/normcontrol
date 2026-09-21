import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from nc5.normative_contract import enrich_catalog, obligations, scope_proof, validate_positive, coverage_summary
from nc5.planning import column_key


def rule(**extra):
    return dict(requirement_id='r',source_id='s',source_sha256='hash',source_quote='Описывается обработка ошибок.',
                document_types=['profile'],document_scope='template',clause='Приложение А, 1',appendix='Приложение А',
                expected_evidence='1 Обработка ошибок',check_stage='sto',parent_context_refs=[],**extra)


def document(text='Данные проверяются по схеме.'):
    blocks=[dict(document='d',locator='p1',text='Обработка ошибок',section='p1',heading_path=['Обработка ошибок'],is_heading=True),
            dict(document='d',locator='p2',text=text,section='p1',heading_path=['Обработка ошибок'])]
    return {'id':'d','blocks':blocks,'headings':[{'locator':'p1','title':'Обработка ошибок','address':'пункт 1 Обработка ошибок'}]}


class NormativeContractTests(unittest.TestCase):
    def test_full_section_proof_and_partial_rejection(self):
        d=document();f={'scope_claim':{'kind':'section','document':'d','sections':['p1']},'evidence':[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]}
        self.assertTrue(scope_proof(f,{'blocks':d['blocks']},[d],rule())['valid'])
        self.assertFalse(scope_proof(f,{'blocks':d['blocks'][1:]},[d],rule())['valid'])

    def test_descendant_not_supplied_prevents_parent_completeness(self):
        d=document();d['blocks'].append(dict(document='d',locator='p3',section='p3',heading_path=['Обработка ошибок','Контроль'],text='Вложенные условия'))
        f={'scope_claim':{'kind':'section','document':'d','sections':['p1']},'evidence':[{'document':'d','locator':'p2'}]}
        self.assertFalse(scope_proof(f,{'blocks':d['blocks'][:2]},[d],rule())['valid'])

    def test_model_cannot_forge_inventory_or_other_document(self):
        d=document();f={'scope_claim':{'kind':'document','document':'d','sections':[]},'evidence':[{'document':'d','locator':'p2'}]}
        self.assertFalse(scope_proof(f,{'blocks':d['blocks'][1:],'source_inventory':{'document_complete':True}},[d],rule())['valid'])
        f['scope_claim']['document']='other'
        self.assertFalse(scope_proof(f,{'blocks':d['blocks']},[d],rule())['valid'])

    def test_external_delegation_and_images_remain_unknown(self):
        d=document('Описание приведено в документе «Руководство».');f={'scope_claim':{'kind':'section','document':'d','sections':['p1']},'evidence':[{'document':'d','locator':'p2'}]}
        self.assertFalse(scope_proof(f,{'blocks':d['blocks']},[d],rule())['valid'])
        d=document();d['coverage']={'images':[{'locator':'p2'}]}
        self.assertFalse(scope_proof(f,{'blocks':d['blocks']},[d],rule())['valid'])

    def test_dependency_table_and_missing_reference(self):
        r=rule();r['source_quote']='Описание приводится по таблице А.1.'
        blocks=[{'locator':'Таблица А.1, строка 1','text':'Функция | Вход | Выход','appendix':'Приложение А'}]
        got=enrich_catalog({'cards':[r]}, {'s':blocks})['cards'][0]
        self.assertEqual(got['normative_dependencies'][0]['quote'],blocks[0]['text'])
        self.assertFalse(got['unresolved_dependencies'])
        self.assertTrue(enrich_catalog({'cards':[r]}, {'s':[]})['cards'][0]['unresolved_dependencies'])

    def test_conditions_and_exceptions_retained(self):
        r=rule();r['source_quote']='При наличии обмена описывается контроль. За исключением тестового контура.'
        result=enrich_catalog({'cards':[r]}, {'s':[]})['cards'][0]
        self.assertEqual(len(result['applicability_contract']['conditions']),2)
        self.assertEqual(result['source_quote'],r['source_quote'])

    def test_composite_list_all_members_need_evidence(self):
        r=rule();r['source_quote']='Указываются:\n- формат;\n- протокол.';r['obligations']=obligations(r)
        self.assertEqual(len(r['obligations']),2)
        d=document();ev=[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]
        row={'state':'checked','outcome':'satisfied','reason':'Подтверждено','checks':[{'obligation_id':o['id'],'state':'checked','outcome':'satisfied','reason':'Есть описание','evidence':ev} for o in r['obligations']]}
        self.assertTrue(validate_positive(row,r,{'blocks':d['blocks']})['positive_evidence_validated'])
        row['checks'].pop()
        self.assertEqual(validate_positive(row,r,{'blocks':d['blocks']})['state'],'insufficient')

    def test_absence_does_not_establish_not_applicable(self):
        r=rule();d=document();row={'state':'not_applicable','outcome':'not_applicable','reason':'Нет','checks':[{'obligation_id':obligations(r)[0]['id'],'state':'not_applicable','outcome':'not_applicable','reason':'Не найдено в выборке','evidence':[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]}]}
        self.assertEqual(validate_positive(row,r,{'blocks':d['blocks']})['state'],'insufficient')

    def test_fabricated_positive_quote_rejected(self):
        r=rule();d=document();row={'state':'checked','checks':[{'obligation_id':obligations(r)[0]['id'],'state':'checked','outcome':'satisfied','reason':'Есть','evidence':[{'document':'d','locator':'p2','quote':'выдуманная цитата'}]}]}
        self.assertEqual(validate_positive(row,r,{'blocks':d['blocks']})['state'],'insufficient')

    def test_split_word_protocol(self):
        self.assertEqual(column_key('Способ взаимодей-ствия/ протокол'),column_key('Способ взаимодействия и протокол'))

    def test_multidocument_coverage_not_merged(self):
        cov=[{'document':'a','requirement_id':'r','state':'checked'},{'document':'b','requirement_id':'r','state':'insufficient'}]
        s=coverage_summary(cov,[])
        self.assertEqual(s['unique_document_requirements'],2)
        self.assertEqual(s['states'],{'checked':1,'unverified':1})

    def test_literal_parenthetical_components_preserved(self):
        r=rule();r['source_quote']='Описываются принципы контроля (форматный, логический) и синхронизации.'
        self.assertEqual([x['quote'] for x in obligations(r)][1:],['форматный','логический'])

    def test_generic_mechanism_is_not_positive_evidence(self):
        r=rule();d=document('Применяются типовые механизмы платформы.')
        row={'state':'checked','checks':[{'obligation_id':obligations(r)[0]['id'],'state':'checked','outcome':'satisfied','reason':'Описано','evidence':[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]}]}
        self.assertEqual(validate_positive(row,r,{'blocks':d['blocks']})['state'],'insufficient')

    def test_checked_violation_is_distinct_from_compliance(self):
        r=rule();d=document('Применяются типовые механизмы платформы.')
        row={'state':'checked','checks':[{'obligation_id':obligations(r)[0]['id'],'state':'checked','outcome':'violated','reason':'Требуемые принципы не раскрыты','evidence':[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]}]}
        self.assertEqual(validate_positive(row,r,{'blocks':d['blocks']})['state'],'checked')

    def test_unspecified_number_is_not_declared_wrong(self):
        self.assertEqual(column_key('№'),'number_unspecified')
        self.assertEqual(column_key('№ связи на схеме'),'number')

    def test_cross_standard_dependency_has_actual_source(self):
        r=rule();r['source_quote']='См. пункт 7.1 СТО РЖД 04.001.2–2021.'
        cat={'cards':[r],'sources':[{'source_id':'other','sha256':'other-hash','standard':'СТО РЖД 04.001.2–2021'}]}
        b={'text':'Должны быть указаны условия.','locator':'абзац 7','clause':'7.1'}
        got=enrich_catalog(cat,{'s':[],'other':[b]})['cards'][0]
        self.assertEqual(got['normative_dependencies'][0]['source_id'],'other')
        self.assertEqual(got['normative_dependencies'][0]['source_sha256'],'other-hash')

    def test_russian_word_forms_retrieve_same_evidence(self):
        from nc5.search import Index
        d=document('Регистрируется журнал ошибок.')
        d['blocks'][0]['text']='Операции'
        for b in d['blocks']:b['heading_path']=['Операции']
        hits=Index([d]).retrieve('ошибками',limit=1,neighbors=0)
        self.assertEqual(hits[0]['locator'],'p2')

    def test_atomic_validator_and_dependency_locator(self):
        from nc5.common import write,digest
        from nc5.catalog_validation import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'sources/source.docx';source.parent.mkdir();source.write_bytes(b'fixture')
            text='Описывается вход. Описывается выход.';quote='Описывается выход.'
            write(source.parent/'extracted.json',{'blocks':[{'locator':'p1','text':text}]})
            r=rule();r.update(source_quote=quote,parent_requirement_id='parent',source_parent_quote=text,source_char_offset=text.index(quote),normative_dependencies=[])
            c={'version':'test','sources':[{'source_id':'s','snapshot':'sources/source.docx','sha256':digest(b'fixture')}], 'cards':[r],
               'ledger':[{'source_id':'s','index':0,'locator':'p1','requirement_ids':['r'],'kind':'обязательное требование','reason':'fixture'}],'profiles':[]}
            with patch('nc5.catalog_validation.DATA',root),patch('nc5.catalog_validation.load_catalog',return_value=c):
                self.assertFalse(audit()['errors'])
                r['normative_dependencies']=[{'source_id':'s','locator':'wrong','quote':text}]
                self.assertEqual(audit()['errors'][0]['error'],'dependency_quote_mismatch')

    def test_engine_recomputes_scope_before_confirming(self):
        from nc5.engine import Engine
        from nc5.store import Store
        from nc5.common import config
        from nc5.checks import validate_finding
        d=document('Обработка ошибок не предусмотрена.')
        for b in d['blocks']:b['address']=b['locator']
        r=rule();r.update(document_name='Контрольный источник',source_locator='rule1',validation_status='synthetic')
        f=validate_finding({'issue':'Не описана обработка ошибок','explanation':'Явный отказ от обработки','suggestion':'Описать обработку','kind':'violation','category':'соответствие СТО','requirement_id':'r',
            'scope_claim':{'kind':'section','document':'d','sections':['p1']},'evidence':[{'document':'d','locator':'p2','quote':d['blocks'][1]['text']}]},[d],{'r':r})
        class Fake:
            def generate(self,p):return {'findings':[],'facts':[],'coverage':[],'limitations':[],'decisions':[{'id':x['id'],'verdict':'confirmed','reason':'Явный отказ противоречит требованию обработки ошибок.','suggestion':'Описать обработку'} for x in p['candidates']]},{'seconds':0}
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(config(),Store(Path(tmp)/'db'));engine.client=Fake()
            for blocks,expected in [(d['blocks'],'confirmed'),(d['blocks'][1:],'question')]:
                j=engine.store.create({'catalog':'synthetic','options':config()});engine.store.update(j,'running');engine.cache.put(j,[d])
                fid=engine.store.finding(j,f,'verifying');candidate={**f,'id':fid}
                engine.store.add(j,'verify',{'stage':'verify','blocks':blocks,'candidates':[candidate],'requirements':[]})
                task=engine.store.claim(j,'verify')
                with patch('nc5.engine.load_catalog',return_value={'cards':[r]}):engine.execute(task)
                self.assertEqual(engine.store.findings(j)[0]['status'],expected)
                saved=engine.store.tasks(j)[0]['result']['raw']['decisions'][0]['reason']
                self.assertNotIn('Недостаточная область',saved)


if __name__=='__main__':unittest.main()
