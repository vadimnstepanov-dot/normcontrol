import unittest
from nc5.common import DEFAULT_CONFIG
from nc5.model import Client, output_budget
from nc5.prompts import COMMON, NUMERIC, NUMERIC_BRIEF, STAGES, POLICY, stage_policy


class PromptContracts(unittest.TestCase):
    def test_brief_numeric_policy_is_confined_to_formula_blocks(self):
        cfg=dict(DEFAULT_CONFIG);client=Client(cfg)
        blocks=[{'document':'d','locator':'p1','text':'Относительный прирост — разность к базовому значению.'},
                {'document':'d','locator':'p0','text':'При отсутствии числителя или знаменателя относительный показатель передаётся как null.'},
                {'document':'d','locator':'p2','text':'100-(D29/D28*100)','table':{'table':1,'column':2}}]
        payload={'stage':'logic','blocks':blocks,'requirements':[]}
        prompt=client.request(payload)['messages'][0]['content']
        self.assertIn(NUMERIC_BRIEF,prompt)
        self.assertIn(COMMON,prompt);self.assertIn(STAGES['logic'],prompt);self.assertIn(NUMERIC,prompt)
        self.assertIn(NUMERIC_BRIEF,client.request({**payload,'stage':'cross'})['messages'][0]['content'])
        for alternative in [{**payload,'stage':'language'},{**payload,'stage':'inter'},
                {**payload,'blocks':blocks+[{'document':'d','locator':'p3','text':'Обработка персональных данных запрещена до создания подсистемы защиты.'}]}]:
            self.assertNotIn(NUMERIC_BRIEF,client.request(alternative)['messages'][0]['content'])
        client=Client({**cfg,'numeric_brief_explanation':False})
        self.assertNotIn(NUMERIC_BRIEF,client.request(payload)['messages'][0]['content'])

    def test_stage_isolation_and_request(self):
        client = Client(dict(DEFAULT_CONFIG))
        for stage in STAGES:
            with self.subTest(stage=stage):
                request = client.request({'stage': stage, 'blocks': [], 'requirements': []})
                system = request['messages'][0]['content']
                self.assertEqual(system, stage_policy(stage))
                self.assertIn(COMMON, system)
                self.assertIn(STAGES[stage], system)
                for other in STAGES:
                    if other != stage:
                        self.assertNotIn(STAGES[other], system)
                self.assertNotEqual(system, POLICY)
                self.assertEqual(request['tools'], [])

    def test_unknown_stage_cannot_silently_use_generic_prompt(self):
        with self.assertRaises(ValueError):
            stage_policy('unrecognized')

    def test_document_text_stays_in_user_data(self):
        marker = 'SYNTHETIC_DOCUMENT_ONLY_73'
        req = Client(dict(DEFAULT_CONFIG)).request({
            'stage': 'sto', 'blocks': [{'document': 'd', 'locator': 'p1', 'text': marker}],
            'requirements': [{'requirement_id': 'r', 'text': 'Обязателен критерий приёмки'}]})
        self.assertNotIn(marker, req['messages'][0]['content'])
        self.assertIn(marker, req['messages'][1]['content'])
        schema = req['response_format']['json_schema']['schema']['properties']
        self.assertEqual(schema['coverage']['maxItems'], 1)
        self.assertEqual(schema['facts']['maxItems'], 0)

    def test_numeric_reasoning_only_on_relevant_stages(self):
        self.assertNotIn(NUMERIC, stage_policy('language'))
        for stage in ('logic', 'sto', 'cross', 'inter', 'verify'):
            self.assertIn(NUMERIC, stage_policy(stage))

    def test_stage_output_budgets_leave_room_for_larger_batches(self):
        cfg=dict(DEFAULT_CONFIG)
        self.assertEqual(output_budget(cfg,{'stage':'language'}),1024)
        self.assertEqual(output_budget(cfg,{'stage':'logic'}),4096)
        self.assertEqual(output_budget(cfg,{'stage':'sto'}),1792)
        self.assertEqual(output_budget(cfg,{'stage':'verify'}),1280)

    def test_logic_budget_still_respects_configured_output_ceiling(self):
        cfg={**DEFAULT_CONFIG,'output':1536}
        self.assertEqual(output_budget(cfg,{'stage':'logic'}),1536)


if __name__ == '__main__':
    unittest.main()
