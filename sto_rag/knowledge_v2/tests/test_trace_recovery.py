import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from knowledge_v2.store import KnowledgeStore,checksum,Conflict
from knowledge_v2.trace import plan,validate_trace
from knowledge_v2.trace_recovery import run,check_plan,validated_seeds,recover_packets
from knowledge_v2.review_wire import TRACE_REFS_VERSION,TRACE_COMPACT_VERSION
from knowledge_v2.tests.test_trace import Model,fixtures


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.docs,self.facts,self.link=fixtures();self.model=Model()
        self.model.wire_version=TRACE_REFS_VERSION
        batches,initial=plan([self.link],self.docs,self.facts,lambda *a:True,self.model)
        base=batches[0]['payload'];self.collect=dict(base,stage='trace_collect',completeness=dict(base['completeness'],full_text=False,evidence_selection=True))
        cid=checksum(self.collect)
        final=dict(base,documents=[],completeness=dict(self.collect['completeness'],collection_parts=1))
        self.plan=dict(initial=initial,batches=[dict(id=cid,payload=self.collect),dict(id=checksum(final),payload=final,collectors=[cid],source_blocks=base['documents'])])
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=KnowledgeStore(Path(self.tmp.name))
    def recover(self,cursor=None,**kwargs):
        cursor={} if cursor is None else cursor
        recover_packets(self.plan,self.model,cursor,lambda:None,lambda:True,**kwargs)
        return cursor
    def test_seeds_are_revalidated_and_never_reinferred(self):
        seed=validate_trace(self.collect,self.model.complete(self.collect));cid=checksum(self.collect);self.model.calls.clear()
        old={'results':{cid:seed},'failures':{'some-old-failure':{'error':'Incomplete model output'}}};before=copy.deepcopy(old)
        check_plan(self.plan,self.docs);seeds=validated_seeds(self.plan,old)
        result=self.recover({'results':seeds});self.assertEqual(old,before)
        self.assertEqual([p['stage'] for p in self.model.calls],['trace_check','trace_verify'])
        self.assertEqual(result['results'][cid],seed)
        old['results'][cid][0]['evidence'][0]['quote']='fabrication'
        with self.assertRaises(ValueError):validated_seeds(self.plan,old)
    def test_failed_collector_splits_exact_scope_and_unions_only_unknown(self):
        original=self.model.complete
        def complete(p):
            if p['stage']=='trace_collect' and len(p['documents'])>1:raise ValueError('Incomplete model output')
            return original(p)
        self.model.complete=complete;cursor=self.recover();cid=checksum(self.collect)
        self.assertEqual(len(cursor['splits']),1)
        self.assertEqual(cursor['results'][cid][0]['outcome'],'unknown')
        calls=[p for p in self.model.calls if p['stage']=='trace_collect']
        self.assertEqual([b for p in calls for b in p['documents']],self.collect['documents'])
        self.assertTrue(all(p['completeness']==self.collect['completeness'] for p in calls))
        self.assertEqual(cursor['failures'],{});self.assertTrue(cursor['history'])
    def test_resume_keeps_finished_child_and_proposed_pass(self):
        original=self.model.complete;state={'fail':True}
        def complete(p):
            if p['stage']=='trace_collect' and len(p['documents'])>1:raise ValueError('Incomplete model output')
            if p['stage']=='trace_collect' and p['documents'][0]['id']=='t' and state['fail']:raise TimeoutError('Queue interruption')
            return original(p)
        self.model.complete=complete;cursor={}
        with self.assertRaises(TimeoutError):self.recover(cursor)
        state['fail']=False;self.recover(cursor)
        self.assertEqual(sum(p['stage']=='trace_collect' and p['documents'][0]['id']=='s' for p in self.model.calls),1)
        self.assertEqual(cursor['failures'],{})
    def test_foreign_alias_and_single_block_failure_are_explicit(self):
        original=self.model.complete
        def complete(p):
            raw=original(p)
            if p['stage']=='trace_collect':raw['decisions'][0]['evidence'][0]['block_id']='outside'
            return raw
        self.model.complete=complete;cursor=self.recover()
        self.assertNotIn(checksum(self.collect),cursor['results'])
        self.assertIn('Incomplete trace collection',str(cursor['failures']))
        self.assertTrue(all(p['stage']=='trace_collect' for p in self.model.calls))
    def test_all_joint_evidence_over_budget_is_not_truncated(self):
        self.model.count=lambda p:20000 if p['stage']=='trace_check' else 100
        cursor=self.recover()
        self.assertIn('none was truncated',str(cursor['failures']))
        self.assertEqual([p['stage'] for p in self.model.calls],['trace_collect'])
    def test_missing_source_or_changed_scope_cannot_be_reused(self):
        changed=copy.deepcopy(self.plan);changed['batches'][-1]['source_blocks'][0]['text']='edited'
        with self.assertRaises(Conflict):check_plan(changed,self.docs)
        changed=copy.deepcopy(self.plan);changed['batches'][0]['payload']['completeness']['gaps']=[{'locator':'missing'}]
        changed['batches'][0]['id']=checksum(changed['batches'][0]['payload'])
        with self.assertRaises(Conflict):check_plan(changed,self.docs)
    def test_task_resume_model_pin_and_candidate_gaps(self):
        self.link['trusted']=False
        # The plan owns the same link object; recreate packet checksums after explicit fixture edit.
        for b in self.plan['batches']:b['id']=checksum(b['payload'])
        self.plan['batches'][-1]['collectors']=[self.plan['batches'][0]['id']]
        self.model.outcome='satisfied';self.model.claim='presence'
        args=(self.store,'recover-job',self.plan,{},self.docs,self.model,lambda:True,lambda *a:False,{'task':'old-task'})
        r=run(*args);self.assertEqual(r['counts'],{'unknown':1});self.assertEqual(r['completed_roots'],2)
        n=len(self.model.calls);self.assertEqual(run(*args)['rows'],r['rows']);self.assertEqual(len(self.model.calls),n)
        self.model.signature='changed'
        with self.assertRaises(Conflict):run(*args)
    def test_revocation_during_generation_cannot_persist(self):
        allowed=[True];self.model.hook=lambda p:allowed.__setitem__(0,False);cursor={}
        with self.assertRaises(PermissionError):recover_packets(self.plan,self.model,cursor,lambda:None,lambda:allowed[0])
        self.assertEqual(cursor['results'],{})
    def test_output_budget_splits_before_call_and_preserves_gaps(self):
        with patch('knowledge_v2.trace_recovery.MAX_BLOCKS',1):cursor=self.recover()
        self.assertEqual(len(cursor['splits']),1)
        self.assertEqual(cursor['root_calls'][checksum(self.collect)],2)
        self.assertEqual(cursor['provenance'][checksum(self.collect)]['kind'],'cpu_evidence_union')
    def test_reuse_verified_final_requires_complete_validated_evidence(self):
        cursor=self.recover();seeds=validated_seeds(self.plan,cursor)
        self.assertEqual(set(seeds),{b['id'] for b in self.plan['batches']})
        cursor['results'][self.plan['batches'][-1]['id']][0]['evidence'][0]['quote']='forged'
        with self.assertRaises(ValueError):validated_seeds(self.plan,cursor)
    def test_v8_counts_verifier_separately_without_double_reservation(self):
        self.model.wire_version=TRACE_COMPACT_VERSION;self.model.context=3000
        self.model.count=lambda p:2100
        cursor=self.recover();self.assertEqual(cursor['failures'],{})
        self.assertEqual([p['stage'] for p in self.model.calls],['trace_collect','trace_check','trace_verify'])
    def test_v8_still_refuses_oversized_actual_verifier(self):
        self.model.wire_version=TRACE_COMPACT_VERSION;self.model.context=3000
        self.model.count=lambda p:2500 if p['stage']=='trace_verify' else 2100
        cursor=self.recover()
        self.assertIn('Trace verification exceeds context',str(cursor['failures']))
        self.assertEqual([p['stage'] for p in self.model.calls],['trace_collect','trace_check'])
