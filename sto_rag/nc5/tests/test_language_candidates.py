import unittest
from nc5.language_candidates import candidates

class LanguageScreenTests(unittest.TestCase):
    def doc(self,rows):return {'id':'test','blocks':[{'locator':'p'+str(i),'text':text,**extra} for i,(text,extra) in enumerate(rows)]}
    def test_missing_bracket_and_typo_have_exact_evidence(self):
        d=self.doc([('Обработка сообщений завершена.',{}),('обработкка сообщений завершена.',{}),('Параметр (описание значения',{})])
        fs=candidates(d)
        self.assertEqual(len(fs),2)
        self.assertTrue(any('обработкка' in f['issue'] for f in fs))
        for f in fs:self.assertEqual(f['evidence'][0]['quote'],d['blocks'][int(f['evidence'][0]['locator'][1:])]['text'])
    def test_numbered_lists_identifiers_and_project_terms_are_not_screened(self):
        d=self.doc([('1) Обработка (процесс) завершена.',{}),('Обработка данных',{}),('обработкка',{'table_context':{'column_name':'Реквизит системы'}}),('Мегаадаптер',{'is_heading':True}),('мегаадаптер обрабатывает данные.',{})])
        self.assertEqual(candidates(d),[])
