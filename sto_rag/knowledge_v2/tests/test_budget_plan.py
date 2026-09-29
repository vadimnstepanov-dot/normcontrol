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

    def test_fitting_whole_document_avoids_boundary_search_and_second_strategy(self):
        rows,blocks,scope=self.inputs([100]*8);model=Budget();model.context=20000;model.output_tokens=1024
        from unittest.mock import patch
        with patch.object(model,'count',wraps=model.count) as calls:
            batches,failed=plan(rows,blocks,model,scope)
        self.assertFalse(failed);self.assertEqual(len(batches),1)
        self.assertEqual(calls.call_count,1)
        self.assert_coverage(rows,blocks,scope,model,batches,failed)

    def test_empty_source_does_not_create_false_complete_review(self):
        rows,_,scope=self.inputs([100]);batches,failed=plan(rows,[],Budget(),scope)
        self.assertFalse(batches);self.assertEqual(failed,rows)

    def test_smaller_groups_win_even_when_no_whole_document_group_fits(self):
        # The former two strategies both chose 8 norms: barely any evidence fit.
        rows,blocks,scope=self.inputs([400]*8,size=30)
        model=Budget();model.output_tokens=1024;model.context=7000
        batches,failed=plan(rows,blocks,model,scope)
        self.assertFalse(failed)
        self.assertLess(len(batches),30)
        self.assert_coverage(rows,blocks,scope,model,batches,failed)

    def test_minimum_calls_matches_exhaustive_contiguous_group_oracle(self):
        rows,blocks,scope=self.inputs([100,1300,400,900,300],size=10)
        model=Budget();model.context=4500;model.output_tokens=1024
        # Exhaustively enumerate every contiguous grouping independently for
        # a one-block document, whose fit has a directly computable cost.
        blocks=[dict(id='only',text='x'*1800,headings=['Whole'])]
        scope['expected_ids']=['only'];reserve=2*model.output_tokens+512
        model.context=6500;best=999
        for cuts in range(1<<(len(rows)-1)):
            groups=[];start=0
            for i in range(len(rows)):
                if i==len(rows)-1 or cuts&(1<<i):groups.append(rows[start:i+1]);start=i+1
            if all(model.count(dict(obligations=g,documents=blocks))+reserve<=model.context for g in groups):best=min(best,len(groups))
        batches,failed=plan(rows,blocks,model,scope)
        self.assertFalse(failed);self.assertEqual(len(batches),best)
        self.assert_coverage(rows,blocks,scope,model,batches,failed)
