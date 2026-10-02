import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nc5.formal_language import findings, duplicate_key, finite_proof
from nc5.store import Store

class FormalLanguageTests(unittest.TestCase):
    def doc(self,text,**extra):
        return {'id':'d','blocks':[{'locator':'p1','text':text,**extra}]}

    def test_real_finite_errors_and_exact_source(self):
        for text in ['Доступ должен осуществляется.','Обновление должно выполнятся.','Система должна функционирует.']:
            fs=list(findings(self.doc(text)))
            self.assertEqual(len(fs),1)
            proof=fs[0]['cpu_proof'];self.assertEqual(text[proof['source_start']:proof['source_end']],fs[0]['evidence'][0]['quote'])
            self.assertFalse(fs[0]['auto_edit_safe'])

    def test_valid_and_quoted_text_is_not_reported(self):
        for text in ['Доступ должен осуществляться.','Оно должно быть обеспечено.','Обновление осуществляется.',
                     'Пример: «должно выполнятся».','Поле `должно выполнятся`.',
                     'Должно, осуществляется по графику.','Обновление должно не выполняться.']:
            self.assertEqual(list(findings(self.doc(text))),[])

    def test_heading_identifier_and_toc_exclusions(self):
        for meta in [{'is_heading':True},{'toc':True},{'table_context':{'column_name':'Идентификатор'}},
                     {'table':{'column_name':'Имя поля'}}]:
            self.assertEqual(list(findings(self.doc('должно выполнятся',**meta))),[])

    def test_ambiguous_clause_boundary_is_not_declared_an_infinitive_error(self):
        for text in ['Тот, кто должен платит вовремя.','Всё как должно осуществляется.']:
            self.assertEqual(list(findings(self.doc(text))),[])

    def test_aspect_is_not_silently_changed(self):
        f=list(findings(self.doc('Обновление должно выполнятся.')))[0]
        self.assertEqual(f['cpu_proof']['infinitives'],['выполняться','выполниться'])
        self.assertIn('выбрать вид',f['suggestion'])

    def test_missing_dictionary_does_not_invent_proof(self):
        finite_proof.cache_clear()
        with patch('nc5.formal_language.morph',return_value=None):
            self.assertEqual(list(findings(self.doc('должно выполнятся'))),[])
        finite_proof.cache_clear()

    def test_cpu_and_model_duplicate_is_confirmed_once_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'db');jid=store.create({})
            cpu=list(findings(self.doc('Обновление должно выполнятся.')))[0]
            fid=store.finding(jid,cpu,'confirmed')
            model={'category':'грамотность','issue':'Ошибочный инфинитив','explanation':'Необходим инфинитив.',
                   'suggestion':'Заменить на выполняться.','requirement_id':'',
                   'evidence':[{'document':'d','locator':'p1','quote':'Обновление должно выполнятся.'}]}
            self.assertEqual(fid,store.finding(jid,model,'candidate'))
            fs=store.findings(jid);self.assertEqual(len(fs),1);self.assertEqual(fs[0]['status'],'confirmed')
            self.assertEqual(fs[0]['explanation'],cpu['explanation'])
            other={**model,'issue':'Лишняя запятая','explanation':'Пунктуационная ошибка.'}
            self.assertNotEqual(fid,store.finding(jid,other,'candidate'))
            self.assertIsNone(duplicate_key({**model,'requirement_id':'norm-1'}))
            self.assertNotEqual(fid,store.finding(jid,{**model,'evidence':[{'document':'d','locator':'p2','quote':'должно выполнятся'}]},'candidate'))

    def test_real_wrong_model_reason_does_not_duplicate_proven_verb_form(self):
        cpu=list(findings(self.doc('Обновление должно выполнятся.')))[0]
        model={'category':'грамотность','issue':'Орфографическая ошибка: неверное написание глагольной формы.',
               'explanation':'В кратких причастиях и деепричастиях пишется суффикс -ся.',
               'suggestion':'Обновление должно выполняться.',
               'evidence':[{'document':'d','locator':'p1','quote':'Обновление должно выполнятся.'}]}
        self.assertEqual(duplicate_key(cpu),duplicate_key(model))
        self.assertIsNone(duplicate_key({**model,'issue':'Неверная глагольная форма и согласование прилагательного'}))
        self.assertIsNone(duplicate_key({**model,'suggestion':'Уточнить глагол.'}))
        self.assertIsNone(duplicate_key({**model,'evidence':[{'document':'d','locator':'p1','quote':'должно выполнятся и должен осуществляется'}]}))
