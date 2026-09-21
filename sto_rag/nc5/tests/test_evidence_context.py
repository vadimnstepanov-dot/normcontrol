import tempfile
import unittest
from pathlib import Path
from nc5.documents import coalesce_groups
from nc5.evidence_context import REFERENCE,reference_payloads,rule_evidence,restore_evidence_addresses,verification_context,symbol_context,local_candidates,link_candidates
from nc5.checks import validate_finding,route_finding
from nc5.quality_gate import assess
from nc5.store import Store

def block(loc,text,section='p1',**kwargs):
    return {'document':'d','locator':loc,'text':text,'section':section,'address':'пункт 2 «Обмен»; абзац 1',**kwargs}

class EvidenceContextTests(unittest.TestCase):
    def test_number_sign_sentence_period_and_no_partial_word(self):
        self.assertEqual([m['number'] for m in REFERENCE.finditer('В Приложении № 12, в таблице 3.2. Приложение содержит текст.')],['12','3.2'])
        self.assertEqual(REFERENCE.search('На рисунке П8.2.')['number'],'П8.2')
    def test_symbol_definition_is_added_from_a_distant_mapping_row(self):
        row={'table':1,'row':7}
        bs=[block('p1','itemName',table_context={**row,'column_name':'Название атрибута в АПИ'}),block('p2','Наименование товара',table_context={**row,'column_name':'Наименование'})]
        group=[{'document':'d','locator':'p99','text':'Поиск по itemName = Поставщики.Наименование','table':{'column_name':'Комментарий'}}]
        selected=symbol_context({'id':'d','blocks':bs},group)
        self.assertEqual({b['locator'] for b in selected},{'p1','p2','p99'})
    def test_mapping_cross_reference_becomes_reviewable_candidate_not_confirmed(self):
        bs=[block('p1','productCode',table_context={'table':1,'row':1,'column_name':'Название атрибута в АПИ'}),
            block('p2','partnerCode',table_context={'table':1,'row':2,'column_name':'Название атрибута в АПИ'}),
            block('p3','Поиск по productCode = Партнеры.Код',table_context={'table':1,'row':2,'column_name':'Комментарий'})]
        items=list(local_candidates({'id':'d','blocks':bs}));self.assertEqual(len(items),1);self.assertEqual(items[0]['kind'],'question')
        validated=validate_finding(items[0],[{'id':'d','blocks':bs}],{})
        self.assertFalse(validated.get('needs_full_scope'))
    def test_appendix_lexical_suspicion_does_not_assert_error(self):
        bs=[block('p1','Схема регистрации заказов приведена в приложении № 4.'),
            block('p20','Схема регистрации заказов',is_heading=True,address='пункт ПРИЛОЖЕНИЕ 3 «Схема регистрации заказов»'),
            block('p30','Отчет по производству',is_heading=True,address='пункт ПРИЛОЖЕНИЕ 4 «Отчет по производству»')]
        candidates=list(link_candidates({'id':'d','blocks':bs}));self.assertEqual(len(candidates),1);self.assertEqual(candidates[0]['kind'],'question')
    def test_appendix_number_from_word_address_and_semantic_target(self):
        bs=[block('p1','Процесс оплаты приведён в приложении № 6.'),
            block('p20','Процесс оплаты',is_heading=True,address='пункт ПРИЛОЖЕНИЕ 5 «Процесс оплаты»; заголовок'),
            block('p30','Процесс возврата',is_heading=True,address='пункт ПРИЛОЖЕНИЕ 6 «Процесс возврата»; заголовок')]
        p=list(reference_payloads({'id':'d','blocks':bs}))[0]
        self.assertEqual({x['number'] for x in p['reference_inventory']},{'5','6'})
        self.assertEqual({x['locator'] for x in p['blocks']},{'p1','p20','p30'})
    def test_exact_unique_locator_repair_is_limited_to_supplied_package(self):
        f={'evidence':[{'document':'d','locator':'p99','quote':'Точная исходная цитата.'}]}
        bs=[block('p1','Точная исходная цитата.')]
        self.assertEqual(restore_evidence_addresses(f,bs)['evidence'][0]['locator'],'p1')
        self.assertEqual(restore_evidence_addresses(f,bs+[block('p2',bs[0]['text'])])['evidence'][0]['locator'],'p99')
        f['evidence'][0]['document']='other'
        self.assertEqual(restore_evidence_addresses(f,bs)['evidence'][0]['locator'],'p99')
    def test_template_section_is_retrieved_even_without_keyword_overlap(self):
        bs=[block('p1','Общие сведения',is_heading=True),block('p2','Заказчик: Альфа.')]
        d={'id':'d','blocks':bs,'headings':[{'locator':'p1','title':bs[0]['text'],'address':'пункт 2 «Общие сведения»; заголовок'}]}
        rule={'document_scope':'template','clause':'Приложение Д, 2','expected_evidence':'Иное название', 'source_quote':'Организация', 'compact_text':'Организация'}
        selected,search=rule_evidence(d,[rule])
        self.assertIn('p2',{b['locator'] for b in selected});self.assertEqual(search['sections'],['p1'])
    def test_large_single_section_does_not_bypass_language_limit(self):
        bs=[block('p'+str(i),'Исходный текст '+str(i)+' '+('слово '*90)) for i in range(40)]
        packs=coalesce_groups({'blocks':bs},target_chars=3000)
        self.assertGreater(len(packs),1);self.assertEqual([b['text'] for p in packs for b in p],[b['text'] for b in bs])
        self.assertTrue(all(sum(len(b['text'])+90 for b in p)<=3000 for p in packs))
    def test_different_contours_are_not_a_confirmed_contradiction(self):
        bs=[block('p1','2 раза в день',table_context={'title':'Обмен продуктивного контура'}),block('p2','1 раз в день',table_context={'title':'Обмен тестового контура'})]
        f={'issue':'Несовпадение периодичности обмена','evidence':[{'document':'d','locator':b['locator'],'quote':b['text']} for b in bs]}
        self.assertEqual(assess(f,[{'id':'d','blocks':bs}])[0],'question')
        bs[1]['table_context']['title']='Обмен продуктивного контура'
        self.assertIsNone(assess(f,[{'id':'d','blocks':bs}])[0])
    def test_api_field_context_protects_against_english_spellcheck(self):
        b=block('p1','recieve_code',table_context={'column_name':'Имя поля'})
        f={'issue':'Опечатка в английском слове','evidence':[{'document':'d','locator':'p1','quote':b['text']}]}
        self.assertEqual(assess(f,[{'id':'d','blocks':[b]}])[0],'question')
    def test_comparing_price_to_line_total_does_not_ignore_quantity(self):
        bs=[block('p'+str(i),text,table_context={'table':1,'row':3,'column_name':col}) for i,(text,col) in enumerate([('2','Количество'),('200','Цена с НДС'),('350','Сумма без НДС'),('50','Сумма НДС')])]
        f={'issue':'Сумма без НДС превышает цену','evidence':[{'document':'d','locator':'p1','quote':'200'},{'document':'d','locator':'p2','quote':'350'}]}
        self.assertEqual(assess(f,[{'id':'d','blocks':bs}])[0],'rejected')
        bs[0]['text']='1';self.assertIsNone(assess(f,[{'id':'d','blocks':bs}])[0])
    def test_fabricated_standard_in_verifier_reason_is_not_accepted(self):
        f={'issue':'Неверная ссылка','explanation':'Нарушает СТО РЖД 01.002, п. 7.','evidence':[{'quote':'См. таблицу 2.'}]}
        self.assertEqual(assess(f)[0],'question')
    def test_local_grammar_is_not_document_absence(self):
        b=block('p1','Сведения должна храниться.')
        f={'category':'грамотность','kind':'style','issue':'Нарушено согласование прилагательного','explanation':'Нет согласования в данной фразе, отсутствует окончание.', 'evidence':[{'document':'d','locator':'p1','quote':b['text']}]}
        f=validate_finding(route_finding(f,'language'),[{'id':'d','blocks':[b]}],{})
        self.assertEqual(f['kind'],'violation');self.assertFalse(f.get('needs_full_scope'))
    def test_reference_duplicates_merge_but_other_defects_remain(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(Path(root)/'db');jid=store.create({})
            f={'category':'техническая логика','issue':'Неверная ссылка на таблицу','suggestion':'Исправить номер.', 'evidence':[{'document':'d','locator':'p1','quote':'См. таблицу 2.1'},{'document':'d','locator':'p2','quote':'Таблица 2.2 — Состав'}]}
            a=store.finding(jid,f);b=store.finding(jid,{**f,'issue':'Несоответствие номера таблицы','suggestion':'Указать 2.2.'})
            self.assertEqual(a,b)
            self.assertNotEqual(a,store.finding(jid,{**f,'issue':'Ошибка в грамматике подписи'}))

if __name__=='__main__':unittest.main()
