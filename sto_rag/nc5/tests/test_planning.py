import unittest,copy,tempfile,zipfile,io
from pathlib import Path
from nc5.planning import atomic_cards,completeness,column_findings,deterministic_rule_check,group_sto_rules,group_sto_evidence,document_registry,focused_outline,compare_bounds,duplicate_groups,comparison_payloads
from nc5.documents import compact_block
class PlanningTests(unittest.TestCase):
 def test_atomic_lists_conditions_and_exact_provenance(self):
  text='Если модуль включен, необходимо описать обмен.\nУказывают поля:\n- код;\n- дата.\nПриводят способ контроля.'
  cards,mapping=atomic_cards([{'requirement_id':'r','check_stage':'sto','source_quote':text,'source_locator':'p1','parent_context_refs':[]}])
  self.assertEqual(len(cards),3)
  self.assertIn('- дата.',cards[1]['source_quote'])
  for c in cards:self.assertEqual(text[c['source_char_offset']:c['source_char_offset']+len(c['source_quote'])],c['source_quote'])
  self.assertTrue(any('Если' in x['quote'] for x in cards[2]['parent_context_refs']))
 def test_completeness_does_not_certify_partial_paragraph(self):
  bs=[{'document':'d','locator':'p1','section':'p1','text':'Длинный абзац','offset':0}];d={'id':'d','blocks':bs}
  self.assertEqual(completeness(d,[{**bs[0],'text':'Длинный'}])['complete_sections'],[])
  self.assertEqual(completeness(d,bs)['complete_sections'],['p1'])
 def test_bounds_compatibility_is_not_implication(self):
  r=compare_bounds('не более 7 часов','не более 9 часов')
  self.assertTrue(r['compatible_if_same_scope']);self.assertFalse(r['right_implies_left_if_same_scope'])
  self.assertTrue(compare_bounds('не более 2 часа','не более 90 минут')['right_implies_left_if_same_scope'])
  self.assertFalse(compare_bounds('не более 2 часа','не менее 3 часа')['compatible_if_same_scope'])
  self.assertEqual(compare_bounds('не более 2 часа и не менее 1 час','не более 3 часа')['state'],'ambiguous')
 def test_duplicate_different_corrections_survive(self):
  a={'id':'a','category':'грамотность','issue':'Ошибка согласования слова','suggestion':'Заменить на форму 1','evidence':[{'document':'d','locator':'p1'}]}
  b={**a,'id':'b','suggestion':'Заменить на форму 2'}
  self.assertEqual(len(duplicate_groups([a,b])[0]),2)
  self.assertEqual(len(duplicate_groups([a,{**a,'id':'b'}])[1]),1)
 def test_header_check_matches_table_identity_and_aliases(self):
  headers=['№ потока/код','Источник данных','Получатель данных','Состав данных','Инициатор','Периодичность']
  bs=[{'document':'d','locator':'p'+str(i),'text':t,'table_context':{'table':1,'row':1,'column':i,'column_name':t,'headers':headers}} for i,t in enumerate(headers)]
  rule={'requirement_id':'r','source_quote':'Таблица со следующими столбцами: «№ связи на схеме», «Источник», «Получатель», «Состав данных», «АС-инициатор взаимодействия», «Периодичность (временной регламент)», «Способ взаимодействия и протокол».'}
  found=column_findings({'id':'d','blocks':bs},rule,[compact_block(b) for b in bs])
  self.assertEqual(len(found),1);self.assertEqual(found[0]['issue'],'В таблице отсутствуют обязательные колонки: Способ взаимодействия и протокол')
  rule['source_quote']=rule['source_quote'].replace(', «Способ взаимодействия и протокол»','')
  self.assertEqual(column_findings({'id':'d','blocks':bs},rule,[compact_block(b) for b in bs]),[])
 def test_closed_table_schema_is_recorded_without_model(self):
  headers=['№ потока','Источник данных','Получатель данных','Состав данных']
  bs=[{'document':'d','locator':'p'+str(i),'text':t,'table_context':{'table':1,'row':1,'column':i,'column_name':t,'headers':headers}} for i,t in enumerate(headers)]
  rule={'requirement_id':'r','source_quote':'Таблица со следующими столбцами: «№ потока», «Источник», «Получатель», «Состав данных».'}
  result=deterministic_rule_check({'id':'d','blocks':bs,'headings':[]},rule,[compact_block(b) for b in bs])
  self.assertEqual(result['state'],'checked');self.assertEqual(result['method'],'deterministic_complete_table_headers')
 def test_sto_rules_share_request_only_for_same_evidence_scope(self):
  base={'document_name':'СТО','appendix':'А','expected_evidence':'4.2 Требования к функциям','normative_kind':'shall','source_quote':'Необходимо описать функцию.'}
  cards=[{**base,'requirement_id':str(i),'clause':str(i)} for i in range(5)]
  cards.append({**base,'requirement_id':'other','expected_evidence':'4.3 Требования к обеспечению'})
  groups=group_sto_rules(cards,4)
  self.assertEqual([len(x) for x in groups],[4,1,1])
 def test_evidence_first_grouping_transmits_shared_source_once(self):
  shared={'document':'d','locator':'p1','offset':0,'text':'Общий набор доказательств'}
  unique={'document':'d','locator':'p2','offset':0,'text':'Отдельное доказательство'}
  rule=lambda rid:{'requirement_id':rid}
  groups=group_sto_evidence([{'rule':rule('a'),'blocks':[shared],'search':{}},{'rule':rule('b'),'blocks':[shared,unique],'search':{}},{'rule':rule('c'),'blocks':[{'document':'d','locator':'p9','text':'Другая область'}],'search':{}}])
  self.assertEqual(sorted(len(x['rules']) for x in groups),[1,2])
  combined=next(x for x in groups if len(x['rules'])==2)
  self.assertEqual(len(combined['blocks']),2)
  self.assertEqual(set(combined['evidence_map']),{'a','b'})
 def test_document_registry_and_focused_outline_are_source_derived(self):
  headings=[{'locator':'p1','title':'1 Общие положения','address':'пункт 1'},{'locator':'p4','title':'2 Надёжность','address':'пункт 2'},{'locator':'p8','title':'3 Другое','address':'пункт 3'}]
  block={'document':'d','locator':'p5','section':'p4','heading_path':['2 Надёжность'],'address':'пункт 2, абзац 1','text':'RPO — допустимая потеря данных. RPO не более 12 часов.'}
  doc={'id':'d','headings':headings,'blocks':[block]}
  registry=document_registry(doc)
  self.assertTrue(any(x['value']=='12' and x['unit'].startswith('час') for x in registry['numbers']))
  self.assertEqual(registry['definitions'][0]['term'],'RPO')
  self.assertLess(len(focused_outline(doc,[block])),len(headings)+1)
 def test_sentence_obligations_are_separate(self):
  cards,_=atomic_cards([{'requirement_id':'r','check_stage':'sto','source_quote':'Описываются интерфейсы. Приводится порядок обработки ошибок.','source_locator':'p1','parent_context_refs':[]}])
  self.assertEqual(len(cards),2)
 def test_row_findings_merge_without_losing_evidence(self):
  from nc5.planning import coalesce_row_findings
  f={'category':'междокументная логика','kind':'question','issue':'Разный состав','explanation':'Пункт отсутствует','suggestion':'Согласовать','evidence':[{'document':'a','locator':'p1','quote':'a'}]}
  g={**f,'issue':'Разный второй пункт','evidence':[{'document':'b','locator':'p2','quote':'b'}]}
  merged=coalesce_row_findings([f,g],{'scope':'complete_matched_table_rows','computed_comparison':{'identity':['row']}})
  self.assertEqual(len(merged),1);self.assertEqual(len(merged[0]['evidence']),2);self.assertEqual(merged[0]['kind'],'question')
 def test_visual_observation_is_not_a_fake_text_quote(self):
  from nc5.vision import anchor_finding
  f=anchor_finding({'evidence':[{'document':'d','locator':'p1','quote':'Надпись внутри схемы'}]},[{'caption':{'document':'d','locator':'p1','text':'Рисунок 1 — Схема'}}])
  self.assertEqual(f['evidence'][0]['quote'],'Рисунок 1 — Схема')
  self.assertEqual(f['visual_evidence'][0]['quote'],'Надпись внутри схемы')
 def test_vision_dedup_and_real_caption(self):
  from PIL import Image
  from nc5.vision import extract,content
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'sample.docx';buf=io.BytesIO();Image.new('RGB',(300,200),'white').save(buf,format='PNG')
   xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:drawing><a:blip r:embed="r1"/></w:drawing></w:p><w:p><w:r><w:t>Рисунок 1 — Схема</w:t></w:r></w:p><w:p><w:drawing><a:blip r:embed="r1"/></w:drawing></w:p></w:body></w:document>'
   with zipfile.ZipFile(path,'w') as z:
    z.writestr('word/document.xml',xml);z.writestr('word/_rels/document.xml.rels','<Relationships><Relationship Id="r1" Target="media/i.png"/></Relationships>');z.writestr('word/media/i.png',buf.getvalue())
   doc={'id':'d','path':str(path),'blocks':[{'document':'d','locator':'p2','text':'Рисунок 1 — Схема'}]}
   assets,issues=extract(doc,Path(temp)/'images');self.assertEqual(len(assets),1);self.assertEqual(assets[0]['occurrences'],['p1','p3']);self.assertFalse(issues)
   self.assertTrue(content(assets[0])['image_url']['url'].startswith('data:image/jpeg;base64,'))
if __name__=='__main__':unittest.main()
