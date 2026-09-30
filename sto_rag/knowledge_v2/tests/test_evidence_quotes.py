import unittest
from knowledge_v2.evidence_quotes import source_quote,attach_source_quotes
from knowledge_v2.review import validate


class EvidenceQuotesTests(unittest.TestCase):
    def test_block_references_get_exact_source_text_without_model_transcription(self):
        documents=[dict(id='b',text='Описание\u00a0решений… и исключения: не менее 48 часов.')]
        decisions=[dict(evidence=[{'block_id':'b'}])]
        attach_source_quotes(decisions,documents)
        self.assertEqual(decisions[0]['evidence'][0]['quote'],documents[0]['text'])
        for bad in ('foreign',None):
            with self.assertRaises(ValueError):attach_source_quotes([dict(evidence=[{'block_id':bad}])],documents)
        with self.assertRaises(ValueError):attach_source_quotes([dict(evidence=[{'block_id':'empty'}])],[dict(id='empty',text=' ')])

    def test_whitespace_only_variants_recover_verbatim_source(self):
        text='Передача:\u00a0не  менее\n48 часов; после согласования.'
        self.assertEqual(source_quote(text,'не менее 48 часов'),'не  менее\n48 часов')
        self.assertEqual(source_quote(text,'Передача: не менее'),'Передача:\u00a0не  менее')
        self.assertEqual(source_quote(text,'48 часов'),'48 часов')

    def test_changed_numbers_words_punctuation_or_ambiguous_span_are_rejected(self):
        text='Не менее\u00a048 часов; после согласования.'
        for quote in ('Не менее 24 часов','Не более 48 часов','Не менее 48 часов,','не менее 48 часов','',None):
            with self.subTest(quote=quote),self.assertRaises(ValueError):source_quote(text,quote)
        with self.assertRaises(ValueError):source_quote('не\u00a0менее / не\tменее','не менее')

    def test_validator_keeps_correct_block_and_stores_exact_quote(self):
        payload={'obligations':[{'id':'r'}],'documents':[dict(id='b',document='doc',locator='p1',text='Не менее\u00a048 часов'),
            dict(id='elsewhere',document='doc',locator='p2',text='24 часа')]}
        decision=dict(obligation_id='r',outcome='unknown',claim='unknown',reason='Проверить',evidence=[{'block_id':'b','quote':'Не менее 48 часов'}])
        result=validate(payload,{'decisions':[decision]})
        self.assertEqual(result[0]['evidence'][0]['quote'],'Не менее\u00a048 часов')
        self.assertEqual(decision['evidence'][0]['quote'],'Не менее 48 часов')
        decision['evidence'][0]['quote']='24 часа'
        with self.assertRaises(ValueError):validate(payload,{'decisions':[decision]})
