import unittest
from knowledge_v2.template_check import extract_templates,compare,with_content

def block(text,n,head=True):
    return dict(exact_text=text,locator='p'+str(n),kind='paragraph',is_heading=head)
def template():
    t=extract_templates({'blocks':[block('Шаблон документа «Описание»',1),block('1 Общие положения',2),block('1.1 Назначение системы',3),block('Текст',4,False)]})[0]
    return dict(t,source_sha256='sha',source_name='СТО',source_ref=['source',1])
def document():
    return dict(id='doc',name='Документ',blocks=[dict(text='1 Общие положения',headings=['1 Общие положения'],locator='p1',location='Раздел 1'),dict(text='1.1 Назначение системы',headings=['1 Общие положения','1.1 Назначение системы'],locator='p2',location='Пункт 1.1')])

class TemplateTests(unittest.TestCase):
    def test_numbered_parent_is_not_false_difference(self):
        rows=compare(document(),[template()],['Описание'])['rows']
        self.assertEqual([r['structure_state'] for r in rows],['matched','matched'])
    def test_rename_is_not_accepted_as_exact_match(self):
        d=document();d['blocks'][1]['text']='1.1 Назначение программы';d['blocks'][1]['headings'][-1]=d['blocks'][1]['text']
        self.assertEqual(compare(d,[template()],['Описание'])['rows'][1]['structure_state'],'difference')
    def test_toc_cannot_prove_presence(self):
        d=document();d['blocks'][1]['text']+='\t5';d['blocks'][1]['headings'][-1]=d['blocks'][1]['text']
        self.assertEqual(compare(d,[template()],['Описание'])['rows'][1]['structure_state'],'question')
    def test_ambiguous_template_and_missing_type_question(self):
        self.assertEqual(compare(document(),[template()],['Другой'])['rows'][0]['structure_state'],'question')
        self.assertEqual(compare(document(),[template(),dict(template(),source_sha256='other')],['Описание'])['rows'][0]['structure_state'],'question')
    def test_extraction_stops_at_next_appendix_ignores_body_numbers(self):
        rows=[block('Шаблон документа «Описание»',1),block('1 Раздел',2),block('2 Пример',3,False),block('Приложение Д',4),block('2 Чужой раздел',5)]
        self.assertEqual(len(extract_templates({'blocks':rows})[0]['elements']),1)
    def test_conditional_element_without_false_violation(self):
        t=template();t['elements'].append(dict(title='Лист регистрации изменений',name='Лист регистрации изменений',number='',locator='p9',locators=['p9'],conditional=True))
        self.assertEqual(compare(document(),[t],['Описание'])['rows'][-1]['structure_state'],'conditional')
    def test_presence_never_proves_content_and_cross_doc_decision_ignored(self):
        c={'documents':[compare(document(),[template()],['Описание'])]}
        wrong=dict(state='checked',obligation=dict(id='o1',document_id='other',source={'sha256':'sha'},atom={'citations':[{'locator':'p2'}]}))
        r=with_content(c,[wrong]);self.assertEqual(r['documents'][0]['rows'][0]['content_state'],'unknown')
        self.assertEqual(c['documents'][0]['rows'][0]['content_state'],'pending')
        wrong['obligation']['document_id']='doc'
        self.assertEqual(with_content(c,[wrong])['documents'][0]['rows'][0]['content_state'],'checked')

if __name__=='__main__':unittest.main()
