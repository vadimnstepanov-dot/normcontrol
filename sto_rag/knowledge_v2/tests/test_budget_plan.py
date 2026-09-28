import copy,unittest
from knowledge_v2.budget_plan import plan


class Budget:
    context=5000
    output_tokens=256
    def count(self,p):
        return 200+sum(r['weight'] for r in p['obligations'])+sum(len(b['text']) for b in p['documents'])


class PlanningTests(unittest.TestCase):
    def inputs(self,weights,size=8):
        rows=[dict(id=str(i),source_revision='same',requirement_ref=['clause'+str(i),1],weight=w) for i,w in enumerate(weights)]
        blocks=[dict(id='b'+str(i),text='x'*300,headings=['Section '+str(i//3)]) for i in range(size)]
        scope=dict(expected_ids=[b['id'] for b in blocks],gaps=[],kind='document')
        return rows,blocks,scope

    def assert_coverage(self,rows,blocks,scope,model,batches,failed):
        for row in rows:
            if row in failed:continue
            tasks=[b['payload'] for b in batches if row in b['payload']['obligations']]
            self.assertEqual({b['id'] for p in tasks for b in p['documents']},set(scope['expected_ids']))
            self.assertEqual(sum(len(p['documents']) for p in tasks),len(blocks))
            for p in tasks:
                self.assertLessEqual(model.count(p)+2*model.output_tokens+512,model.context)
                self.assertEqual(p['completeness']['full_text'],len(tasks)==1)

    def test_every_requirement_keeps_exact_complete_scope_under_varied_budgets(self):
        for context in (2100,3500,5000,9000):
            for weights in ([100]*9,[100,2300,900,4200,100]):
                with self.subTest(context=context,weights=weights):
                    rows,blocks,scope=self.inputs(weights);before=copy.deepcopy((rows,blocks,scope))
                    model=Budget();model.context=context
                    batches,failed=plan(rows,blocks,model,scope)
                    self.assertEqual((rows,blocks,scope),before)
                    self.assert_coverage(rows,blocks,scope,model,batches,failed)

    def test_complete_document_strategy_reduces_calls_when_large_group_splits(self):
        rows,blocks,scope=self.inputs([1200,1200,1200]);model=Budget()
        batches,failed=plan(rows,blocks,model,scope)
        self.assertFalse(failed);self.assertEqual(len(batches),3)
        self.assertTrue(all(b['payload']['completeness']['full_text'] for b in batches))

    def test_oversized_block_is_explicit_no_partial_plan_for_its_obligation(self):
        rows,blocks,scope=self.inputs([100]);blocks[-1]['text']='x'*10000
        batches,failed=plan(rows,blocks,Budget(),scope)
        self.assertFalse(batches);self.assertEqual(failed,rows)

    def test_empty_source_does_not_create_false_complete_review(self):
        rows,_,scope=self.inputs([100]);batches,failed=plan(rows,[],Budget(),scope)
        self.assertFalse(batches);self.assertEqual(failed,rows)
