import copy
import json
from pathlib import Path
import tempfile
import unittest

from knowledge_v2.review import ReviewRunner, aggregate, compound_decisions, corpus, ledger, plan, validate, request
from knowledge_v2.store import KnowledgeStore, Conflict, NotReady, checksum
from .test_ingest import docx


class Model:
    signature = 'model-a'
    context = 16000
    output_tokens = 256
    def __init__(self): self.calls=[]; self.hook=None; self.claim='contradiction'
    def count(self, payload): return len(json.dumps(payload, ensure_ascii=False))//4
    def complete(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.hook: self.hook()
        evidence = [] if self.claim == 'absence' else [{'block_id':payload['documents'][0]['id'], 'quote':payload['documents'][0]['text']}]
        return {'decisions':[dict(obligation_id=r['id'], outcome='violated', claim=self.claim,
            reason='Контрольный конфликт', evidence=evidence) for r in payload['obligations']]}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(Path(self.tmp.name)/'knowledge');self.model=Model();self.allowed=True
        self.runner=ReviewRunner(self.store,self.model,lambda sid:self.allowed)
        self.path=Path(self.tmp.name)/'test.docx';docx(self.path, 'Система хранит журнал 2 дня.')
        self.did=corpus([self.path])[0]['id']
        self.store.register_set('set','scope',{})
        self.add('source_revision','source',dict(sha256='a'*64,original_key='originals/test.docx',parser_version='test'))
        self.add('fragment','f',dict(source_revision=['source',1],locator='3.1',exact_text='Система должна хранить журнал 30 дней.',search_text='журнал',context_hash='hash'))
        expression={'fact':{'name':'type','in':['test']}}
        self.add('profile','profile',dict(source_revision=['source',1],definition=dict(id='profile',name='Test',expression=expression,basis=[{'locator':'3.1'}],version=1)))
        self.add('requirement','r',dict(fragment_refs=[['f',1]],source_revision=['source',1],modality='mandatory',condition=expression,
            card=dict(effective_profile_id='profile',state='validated',validation={'provenance':{'status':'verified'},'completeness':{'semantic':'verified_simple'}})))
        for oid in ('a','b'):
            self.add('obligation',oid,dict(requirement_ref=['r',1],subject='Система',action='хранить',object='журнал',citation={'locator':'3.1','quote':'Система должна хранить журнал 30 дней.'}))
        with self.store.connection() as db: refs=[(r['id'],r['version']) for r in db.execute('SELECT id,version FROM records')]
        manifest=self.store.create_release('set','release','generation','embedding',refs,{'extractor':'test'})
        self.store.attest_ready('release',dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(manifest),embedding_space='embedding',record_count=len(refs),watermark=1))
        self.store.apply_command('publish','release.publish',dict(set_id='set',release_id='release',manifest_hash=checksum(manifest)))
        self.facts={self.did:{'type':{'value':'test','evidence':[{'source':self.did,'locator':'p1'}]}}}

    def add(self, kind, rid, payload): self.store.put_record('set',kind,rid,1,payload)
    def create(self): return self.runner.create([self.path],{self.did:{'release':['profile']}},self.facts,lambda *args:True)

    def test_end_to_end_full_ledger_shared_text_and_verifier_scope(self):
        task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertEqual(report['normative_coverage']['total'],2)
        self.assertEqual(report['violation_count'],1)
        self.assertEqual(len(self.model.calls),2)
        check,verify=self.model.calls
        self.assertEqual(len(check['documents']),4)
        self.assertEqual(check['completeness'],verify['completeness'])
        self.assertEqual(check['documents'],verify['documents'])
        self.assertEqual(report['snapshot']['releases'][0]['embedding_space'],'embedding')

    def test_no_fallback_no_unpublished_release(self):
        self.store.revoke_release('release','test')
        with self.assertRaises(NotReady):self.create()

    def test_revoked_acl_blocks_processing_and_report(self):
        task=self.create();self.allowed=False
        with self.assertRaises(PermissionError):self.runner.run_once()
        with self.assertRaises(PermissionError):self.runner.report(task)
        self.assertEqual(self.model.calls,[])

    def test_revoked_between_calls(self):
        self.create();self.model.hook=lambda:setattr(self,'allowed',False)
        with self.assertRaises(PermissionError):self.runner.run_once()
        self.assertEqual(len(self.model.calls),1)

    def test_pause_resume_without_reset(self):
        task=self.create();self.runner.pause(task);self.runner.run_once()
        self.assertEqual(self.model.calls,[])
        self.runner.pause(task,False);self.runner.run_once()
        self.assertEqual(self.runner.report(task)['state'],'done')
        self.assertFalse(self.runner.run_once())
        self.assertEqual(len(self.model.calls),2)

    def test_model_change_refuses_resume(self):
        self.create();self.model.signature='model-b'
        with self.assertRaises(Conflict):self.runner.run_once()

    def test_forged_quote_rejected(self):
        task=self.create()
        with self.store.connection() as db: p=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task,)).fetchone()[0])['batches'][0]['payload']
        bad=self.model.complete(p);bad['decisions'][0]['evidence'][0]['quote']='выдуманная цитата'
        with self.assertRaises(ValueError):validate(p,bad)

    def test_missing_obligation_rejected(self):
        task=self.create()
        with self.store.connection() as db:p=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task,)).fetchone()[0])['batches'][0]['payload']
        bad=self.model.complete(p);bad['decisions'].pop()
        with self.assertRaises(ValueError):validate(p,bad)

    def test_missing_group_decision_splits_without_reducing_evidence_scope(self):
        original=self.model.complete
        def omit(p):
            result=original(p)
            if len(p['obligations'])>1:result['decisions'].pop()
            return result
        self.model.complete=omit
        task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertFalse(report['errors']);self.assertEqual(report['state'],'done')
        calls=self.model.calls
        self.assertTrue(any(len(c['obligations'])==1 for c in calls))
        self.assertTrue(all(c['documents']==calls[0]['documents'] and c['completeness']==calls[0]['completeness'] for c in calls))

    def test_incomplete_partition_cannot_prove_absence(self):
        task=self.create()
        with self.store.connection() as db:p=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task,)).fetchone()[0])
        self.model.claim='absence';b=p['batches'][0]
        result={b['id']:{'decisions':validate(b['payload'],self.model.complete(b['payload']))}}
        missing=copy.deepcopy(b);missing['id']='missing'
        ds=aggregate(p['rows'],[b,missing],result,[],p['scopes'][self.did])
        self.assertTrue(all(d['state']=='unknown' and not d['global_absence_proven'] for d in ds))
        scope=dict(p['scopes'][self.did],gaps=[{'reason':'unread image'}])
        ds=aggregate(p['rows'],[b],result,[],scope)
        self.assertTrue(all(d['state']=='unknown' for d in ds))

    def test_unknown_applicability_is_not_checked(self):
        self.facts={};task=self.create();self.runner.run_once()
        self.assertEqual(self.runner.report(task)['normative_coverage']['unknown'],2)

    def test_unknown_is_saved_without_a_second_model_call(self):
        self.model.complete=lambda p:dict(decisions=[dict(obligation_id=r['id'],outcome='unknown',claim='unknown',reason='Нет достаточных данных',evidence=[]) for r in p['obligations']])
        calls=[];original=self.model.complete
        self.model.complete=lambda p:(calls.append(p) or original(p))
        task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertEqual(len(calls),1);self.assertEqual(report['normative_coverage']['unknown'],2)
        self.assertFalse(any(d['global_absence_proven'] for d in report['decisions']))

    def test_production_timings_persist_without_extra_model_calls(self):
        original=self.model.complete
        def timed(p):
            self.model.last_usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
            self.model.last_timings={'prompt_ms':50,'predicted_ms':1000,'predicted_per_second':20,'private_path':'never persist'}
            return original(p)
        self.model.complete=timed;task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertEqual(len(self.model.calls),2);self.assertEqual(report['performance']['model_calls'],2)
        self.assertEqual(report['performance']['completion_tokens'],40)
        self.assertEqual(report['performance']['predicted_ms'],2000)
        with self.store.connection() as db:cursor=json.loads(db.execute('SELECT cursor FROM tasks WHERE id=?',(task,)).fetchone()[0])
        self.assertNotIn('private_path',str(cursor))

    def test_only_decisive_proposals_are_independently_verified(self):
        original=self.model.complete
        def mixed(p):
            result=original(p)
            if p['stage']=='check':result['decisions'][0].update(outcome='unknown',claim='unknown',reason='Неопределённость')
            return result
        self.model.complete=mixed;task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertEqual(len(self.model.calls),2)
        self.assertEqual(len(self.model.calls[0]['obligations']),2)
        self.assertEqual(len(self.model.calls[1]['obligations']),1)
        self.assertEqual(self.model.calls[0]['documents'],self.model.calls[1]['documents'])
        self.assertEqual(report['normative_coverage']['unknown'],1)

    def test_oversized_is_explicit_not_truncated(self):
        self.model.context=1100;task=self.create();self.runner.run_once()
        report=self.runner.report(task)
        self.assertEqual(report['normative_coverage']['unknown'],2)
        self.assertTrue(all(d['issues'] for d in report['decisions']))
        self.assertEqual(self.model.calls,[])

    def test_different_owner_cannot_read_or_claim(self):
        task=self.create()
        other=ReviewRunner(self.store,self.model,lambda sid:True,owner='other')
        self.assertFalse(other.run_once())
        with self.assertRaises(PermissionError):other.report(task)
        with self.assertRaises(PermissionError):other.pause(task)

    def test_paused_job_does_not_block_next_job(self):
        first=self.create();second=self.create();self.runner.pause(first)
        self.runner.run_once()
        self.assertEqual(self.runner.report(first)['state'],'pending')
        self.assertEqual(self.runner.report(second)['state'],'done')

    def test_alternative_is_not_a_false_violation(self):
        task=self.create();self.runner.run_once();members=self.runner.report(task)['decisions']
        for m in members:m['obligation']['composition']={'any_of':[0,1]}
        members[0]['state']='checked'
        self.assertEqual(compound_decisions(members)[0]['state'],'checked')
        members[0]['state']='unknown'
        self.assertEqual(compound_decisions(members)[0]['state'],'unknown')

    def test_full_text_partition_union_is_required(self):
        task=self.create();self.model.claim='absence';self.runner.run_once()
        with self.store.connection() as db:r=db.execute('SELECT payload,cursor FROM tasks WHERE id=?',(task,)).fetchone()
        p,c=json.loads(r[0]),json.loads(r[1]);scope=p['scopes'][self.did]
        scope['expected_ids'].append('unsubmitted-block')
        result=aggregate(p['rows'],p['batches'],c['results'],[],scope)
        self.assertTrue(all(d['state']=='unknown' for d in result))

    def test_failed_json_is_bounded_and_visible(self):
        self.model.complete=lambda payload:{'decisions':[]}
        task=self.create();self.runner.run_once();report=self.runner.report(task)
        self.assertEqual(report['state'],'partial')
        self.assertTrue(report['errors'])
        self.assertEqual(report['violation_count'],0)

    def test_truncated_group_splits_same_scope_and_resumes_saved_child(self):
        original=self.model.complete;blocked=[True]
        def limited(p):
            if len(p['obligations'])>1:raise ValueError('Incomplete model output')
            if p['obligations'][0]['obligation_id']=='b' and blocked[0]:raise RuntimeError('Temporary transport failure')
            return original(p)
        self.model.complete=limited;task=self.create();self.runner.run_once(task)
        self.assertEqual(self.runner.report(task)['state'],'partial')
        first_calls=len(self.model.calls);blocked[0]=False
        self.runner.pause(task,False);self.runner.run_once(task)
        self.assertEqual(self.runner.report(task)['state'],'done')
        self.assertEqual(len(self.model.calls)-first_calls,2)
        self.assertTrue(all(p['completeness']['full_text'] for p in self.model.calls))

    def test_report_available_with_model_off_and_revoked_release(self):
        task=self.create();self.runner.run_once();self.store.revoke_release('release','superseded')
        reader=ReviewRunner(self.store,None,lambda sid:True)
        self.assertEqual(reader.report(task)['state'],'done')

    def test_explicit_retry_keeps_snapshot_and_recovers_errors(self):
        original=self.model.complete
        self.model.complete=lambda payload:{'decisions':[]}
        task=self.create();before=self.runner.report(task)['snapshot'];self.runner.run_once()
        self.model.complete=original
        self.runner.pause(task,False);self.runner.run_once()
        self.assertEqual(self.runner.report(task)['state'],'done')
        self.assertEqual(self.runner.report(task)['snapshot'],before)
        self.assertFalse(self.runner.report(task)['errors'])


if __name__=='__main__':unittest.main()
