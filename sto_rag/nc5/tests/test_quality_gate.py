import unittest
from nc5.checks import route_finding
from nc5.quality_gate import assess


class QualityGateTests(unittest.TestCase):
    def test_explicit_template_rule_cannot_be_dismissed_by_similarity(self):
        from nc5.quality_gate import assess_rejection
        f={'template_source':{'title':'Описание объекта'},'source':{'source_quote':'Не допускается удалять или переименовывать пункты шаблона.'}}
        self.assertEqual(assess_rejection(f,'Точное совпадение названия не является обязательным, смысл сохранён.')[0],'question')
        self.assertIsNone(assess_rejection(f,'Этот шаблон относится к другому типу документа; применимость не установлена.')[0])
    def test_repeated_project_term_is_protected_before_replacement_similarity(self):
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'Номенклатор ePrica','is_heading':True},{'locator':'p2','text':'Работа с номенклатором.'},{'locator':'p3','text':'Данные номенклатора.'}]}]
        f={'issue':'Опечатка в названии «Номенклатор»','explanation':'Пропущена буква.', 'suggestion':'Заменить на «классификатор»',
           'evidence':[{'document':'d','locator':'p1','quote':'Номенклатор ePrica'}]}
        self.assertEqual(assess(f,docs)[0],'question')
        f['evidence'][0]['quote']='притвязкой';self.assertIsNone(assess(f,docs)[0])
        f.update(issue='Опечатка «притвязкой»',explanation='Лишняя буква т.',suggestion='с привязкой к Номенклатору')
        f['evidence'][0]['quote']='притвязкой к Номенклатору';self.assertIsNone(assess(f,docs)[0])

    def test_cyrillic_camelcase_requisite_is_not_prose(self):
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'КодОКПД2','table_context':{'column_name':'Реквизит номенклатуры'}}]}]
        f={'issue':'Отсутствует пробел','explanation':'Нужно разделять слова.',
           'evidence':[{'document':'d','locator':'p1','quote':'КодОКПД2'}]}
        self.assertEqual(assess(f,docs)[0],'question')
        docs[0]['blocks'][0]['table_context']['column_name']='Комментарий'
        self.assertIsNone(assess(f,docs)[0])

    def test_touching_dates_do_not_prove_schedule_conflict(self):
        f={'issue':'Пересечение сроков этапов','explanation':'Периоды накладываются.',
           'evidence':[{'quote':'с 01.09.2026 по 05.09.2026'},{'quote':'с 05.09.2026 по 12.09.2026'}]}
        self.assertEqual(assess(f)[0],'question')
        f['evidence'][1]['quote']='с 04.09.2026 по 12.09.2026'
        self.assertIsNone(assess(f)[0])

    def test_confirmed_reason_cannot_itself_request_question_status(self):
        f={'issue':'Низкое разрешение','explanation':'Вероятно, опечатка; статус question является корректным.', 'evidence':[{'quote':'не менее 350х400'}]}
        self.assertEqual(assess(f)[0],'question')

    def test_false_grammar_explanation_is_blocked(self):
        f={'issue':'Нарушение управления','explanation':'Предлог «в» требует творительного падежа.', 'evidence':[{'quote':'в Таблица 6.1'}]}
        self.assertEqual(assess(f)[0],'question')
        f['explanation']='Подлежащее «Целью» требует причастия в женском роде.'
        self.assertEqual(assess(f)[0],'question')
        f['explanation']='Подлежащим в предложении является «Целью» (женский род).'
        self.assertEqual(assess(f)[0],'question')
        f['explanation']='Глагол «управление» требует родительного падежа.'
        self.assertEqual(assess(f)[0],'question')
        f['explanation']='Предлог «в» требует предложного падежа в значении места.'
        self.assertIsNone(assess(f)[0])

    def test_reference_defect_and_broken_sentence_are_not_style(self):
        for title in ('Неверная ссылка на таблицу 3.1','Обрыв фразы в перечне','Неверное управление в предложении'):
            f=route_finding({'issue':title,'explanation':'','kind':'style'},'language')
            self.assertEqual(f['kind'],'violation')
        self.assertEqual(route_finding({'issue':'Неверная ссылка на таблицу 3.1','explanation':'','kind':'style'},'language')['category'],'межраздельная логика')

    def test_general_template_rule_is_not_a_numbering_citation(self):
        from nc5.checks import validate_finding
        docs=[{'id':'d','blocks':[{'locator':'p1','text':'Сведения приведены в таблице 6.1.','address':'Раздел 6, абзац 1'}]}]
        f=route_finding({'category':'структура СТО','kind':'violation','issue':'Неверная ссылка на таблицу 6.1','explanation':'Номер не совпадает.', 'requirement_id':'r','evidence':[{'document':'d','locator':'p1','quote':'таблице 6.1'}]},'sto')
        card=dict(requirement_id='r',document_name='СТО',clause='7',appendix='',source_locator='p7',source_quote='Не допускается переименовывать разделы шаблона.',source_sha256='x',validation_status='source_exact')
        result=validate_finding(f,docs,{'r':card})
        self.assertEqual(result['requirement_id'],'');self.assertNotIn('source',result)
        self.assertEqual(result['unverified_normative_source']['clause'],'7')
        card['source_quote']='Ссылки на таблицы должны соответствовать их номерам.'
        self.assertEqual(validate_finding(f,docs,{'r':card})['requirement_id'],'r')

    def test_single_rule_response_cannot_emit_many_coverage_decisions(self):
        from nc5.common import config
        from nc5.model import Client
        schema=Client(config()).request({'stage':'sto','blocks':[],'requirements':[{'requirement_id':'r'}]})['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['coverage']['maxItems'],1)
        schema=Client(config()).request({'stage':'verify','blocks':[],'candidates':[{'id':'f'}]})['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['decisions']['maxItems'],1)
        self.assertIn('suggestion',schema['properties']['decisions']['items']['required'])

    def test_unknown_term_claim_is_not_a_dictionary(self):
        f={'category':'грамотность','issue':'Ошибка в термине',
           'explanation':'Это неологизм, поэтому его надо заменить.',
           'evidence':[{'quote':'Данные поступают в адаптер Зетапро.'}]}
        self.assertEqual(assess(f)[0],'question')

    def test_ambiguous_noun_gender_requires_context(self):
        f={'category':'грамотность','issue':'Неправильный род существительного «спазма»',
           'explanation':'Следует использовать только мужской род.',
           'evidence':[{'quote':'Спазма прошла.'}]}
        self.assertEqual(assess(f)[0],'question')

    def test_objective_spelling_claim_is_sent_to_verification(self):
        f={'category':'грамотность','kind':'style','issue':'Орфографическая ошибка',
           'explanation':'В слове пропущена буква.'}
        self.assertEqual(route_finding(f,'language')['kind'],'violation')

    def test_technical_inconsistency_is_not_relabelled_as_grammar(self):
        f={'category':'техническая логика','kind':'violation','issue':'Несогласованность ёмкости хранилища',
           'explanation':'Объём данных превышает доступную ёмкость.'}
        self.assertEqual(route_finding(f,'logic')['category'],'техническая логика')
    def test_bound_uses_condition_and_unit_from_the_same_table_row(self):
        row={'table':2,'row':7}
        def block(loc,text,column):return {'locator':loc,'text':text,'table_context':{**row,'column_name':column}}
        docs=[{'id':'d','blocks':[block('p1','9','Эталонное значение'),block('p2','не более','Условие'),block('p3','сек','Единица измерения')]}]
        f={'issue':'Противоречие времени отклика','explanation':'Различаются значения.',
            'evidence':[{'document':'d','locator':'p1','quote':'9'},{'document':'d','locator':'p4','quote':'Время отклика — не более 17 секунд.'}]}
        self.assertEqual(assess(f,docs)[0],'question')
        docs[0]['blocks'][1]['table_context']['row']=8
        self.assertIsNone(assess(f,docs)[0])
    def test_api_spelling_requires_contract_but_mixed_alphabet_remains_checkable(self):
        f={'issue':'Опечатка в идентификаторе атрибута','explanation':'Неверное английское написание.',
            'evidence':[{'quote':'recieveCode'}],'suggestion':'receiveCode'}
        self.assertEqual(assess(f)[0],'question')
        f['evidence'][0]['quote']='rеceiveCode'
        self.assertIsNone(assess(f)[0])


if __name__=='__main__':unittest.main()
