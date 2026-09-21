import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nc5.common import config
from nc5.model import Client, OutputError
from nc5.evidence_context import rule_evidence

class QualityRevision(unittest.TestCase):
    def test_unknown_term_requires_lexical_evidence(self):
        from nc5.quality_gate import assess
        f={'issue':'Неверное словоупотребление','explanation':'Слово не является нормативным термином в данном контексте.','evidence':[{'quote':'Термин проекта'}]}
        self.assertEqual(assess(f)[0],'question')
    def test_directional_verb_allows_accusative(self):
        from nc5.quality_gate import assess
        f={'issue':'Неправильное управление предлогом','explanation':'Требуется предложный падеж.','suggestion':'В группе входят сотрудники.','evidence':[{'quote':'В группу входят сотрудники.'}]}
        self.assertEqual(assess(f)[0],'question')
    def test_quote_outer_space_can_be_removed_but_text_cannot_be_rewritten(self):
        from nc5.evidence_context import restore_evidence_addresses
        bs=[{'document':'d','locator':'p1','text':'Передача сведений'}]
        f={'evidence':[{'document':'d','locator':'p1','quote':'Передача сведений '}]}
        fixed=restore_evidence_addresses(f,bs)['evidence'][0]
        self.assertEqual(fixed['quote'],'Передача сведений')
        self.assertEqual(fixed['original_quote'],'Передача сведений ')
        f['evidence'][0]['quote']='Передача  сведений '
        self.assertEqual(restore_evidence_addresses(f,bs)['evidence'][0]['quote'],'Передача  сведений ')
    def test_format_preference_without_standard_is_style(self):
        from nc5.quality_gate import assess
        f={'issue':'Нарушение единообразия форматирования списка','evidence':[{'quote':'Перечень объектов'}]}
        self.assertEqual(assess(f)[0],'style')
        self.assertIsNone(assess({**f,'requirement_id':'rule'})[0])
    def test_receiver_may_initiate_exchange(self):
        from nc5.quality_gate import assess
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'Система Б','table_context':{'column_name':'Инициатор обмена'}}]}]
        f={'issue':'Несогласованность ролей','explanation':'Получатель не может быть инициатором, если источник другая система.','evidence':[{'document':'d','locator':'p1','quote':'Система Б'}]}
        self.assertEqual(assess(f,docs)[0],'question')

    def test_unnamed_repeated_term_is_not_automatically_renamed(self):
        from nc5.quality_gate import assess
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'Номенклатор','is_heading':True},{'locator':'p2','text':'Работа с номенклатором'},{'locator':'p3','text':'Состав номенклатора'}]}]
        f={'issue':'Орфографическая ошибка','explanation':'В написании пропущена буква.','suggestion':'Исправить на номенклатуром.','evidence':[{'document':'d','locator':'p2','quote':'Работа с номенклатором'}]}
        self.assertEqual(assess(f,docs)[0],'question')
    def test_cross_stage_duplicate_requires_same_correction_and_quotes(self):
        from nc5.planning import duplicate_groups
        a={'category':'техническая логика','issue':'Несогласованное обозначение','suggestion':'Использовать единое обозначение компонента во всём разделе.','evidence':[{'document':'d','locator':'p1','quote':'Компонент А'}]}
        b={**a,'category':'межраздельная логика','issue':'Различие наименований'}
        self.assertEqual(len(duplicate_groups([a,b])[0]),1)
        self.assertEqual(len(duplicate_groups([a,{**b,'suggestion':'Уточнить версию компонента.'}])[0]),2)
        self.assertEqual(len(duplicate_groups([a,{**b,'evidence':[{'document':'d','locator':'p1','quote':'Компонент Б'}]}])[0]),2)
    def test_length_retry_preserves_evidence_and_counts_both_attempts(self):
        c=Client(config()); c.count=lambda p:100
        requests=[]
        def http(path,req):
            requests.append(req)
            return {'choices':[{'finish_reason':'length' if len(requests)==1 else 'stop','message':{'content':json.dumps({k:[] for k in ('findings','facts','coverage','decisions','limitations')})}}], 'usage':{'completion_tokens':10}, 'timings':{'predicted_n':10,'predicted_ms':100}}
        c.http=http
        with tempfile.TemporaryDirectory() as tmp, patch('nc5.common.DATA',Path(tmp)):
            _,metrics=c.generate({'stage':'logic','blocks':[{'document':'d','locator':'p1','text':'Исходный раздел'}]})
        self.assertEqual([r['max_tokens'] for r in requests],[2048,4096])
        self.assertEqual(metrics['usage']['completion_tokens'],20)
        self.assertEqual(metrics['timings']['predicted_n'],20)
        self.assertEqual(json.loads(requests[0]['messages'][1]['content'])['blocks'],json.loads(requests[1]['messages'][1]['content'])['blocks'])

    def test_no_retry_if_context_has_no_room(self):
        c=Client(config());c.context=2048+1536+100;c.count=lambda p:100
        calls=[]
        c.http=lambda *a: calls.append(a) or {'choices':[{'finish_reason':'length','message':{'content':''}}]}
        with tempfile.TemporaryDirectory() as tmp, patch('nc5.common.DATA',Path(tmp)):
            with self.assertRaises(OutputError):c.generate({'stage':'logic'})
        self.assertEqual(len(calls),1)

    def test_general_front_matter_is_not_limited_to_keyword_hits(self):
        blocks=[{'document':'d','locator':f'p{i}','section':'','text':f'Организация {i}'} for i in range(25)]
        doc={'id':'d','blocks':blocks,'headings':[]}
        selected,_=rule_evidence(doc,[{'document_scope':'general','clause':'3.2','expected_evidence':'Титульный лист','source_quote':'Обозначение документа'}])
        self.assertEqual({b['locator'] for b in selected},{b['locator'] for b in blocks})

    def test_general_clause_number_is_not_document_section_number(self):
        blocks=[{'document':'d','locator':'p1','section':'p1','text':'Независимый раздел','is_heading':True}]
        _,search=rule_evidence({'id':'d','blocks':blocks,'headings':[{'locator':'p1','title':'Независимый раздел','address':'пункт 7 «Раздел»'}]},[{'document_scope':'general','clause':'СТО, 7','expected_evidence':'Титульный лист','source_quote':'Обозначение'}])
        self.assertEqual(search['sections'],[])
