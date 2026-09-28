import copy
import json
import unittest
from knowledge_v2.experience import apply, ExperienceSelector
from knowledge_v2.review import ReviewRunner, corpus
from .test_ingest import docx
from knowledge_v2.search import GenerationBuilder, HybridSearch
from knowledge_v2.store import checksum, Conflict, NotReady
from . import test_review
from .test_search import FakeEncoder, FakeVectors


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_review.ReviewTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store
        self.task=self.fixture.create();self.fixture.runner.run_once()
        report=self.fixture.runner.report(self.task)
        self.obligation=report['decisions'][0]['obligation']['id']
        with self.store.connection() as db:p=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(self.task,)).fetchone()[0])
        block=p['documents'][0]['blocks'][1]
        self.store.register_set('lessons','private-scope',{})
        self.draft=dict(summary='При проверке срока хранения журнала учитывать среду эксплуатации.',
            conditions={'fact':{'name':'environment','in':['production']}},
            counter_conditions={'fact':{'name':'environment','in':['test']}},
            counterexample='Тестовая среда с отдельным нормативным сроком.',required_evidence='Срок, среда и точный пункт нормы.',
            stages=['check','verify','sto','logic','language','inter'],scope_id='private-scope',
            normative_refs=[dict(set_id='set',release_id='release',requirement_ref=['r',1])],sharing_confirmed=True)
        self.submission=dict(set_id='lessons',actor_id='local',proposal_id='proposal',task_id=self.task,
            obligation_id=self.obligation,proposal_kind='private_example',comment='Срок необходимо сопоставлять только в одной среде.',
            evidence=[dict(block_id=block['id'],quote=block['text'])],draft=self.draft)
        self.granted=True
        self.authorize=lambda actor,sid,action:self.granted
        self.encoder,self.vector=FakeEncoder(),FakeVectors()
        self.search=HybridSearch(self.store,self.encoder,self.vector,lambda sid:self.granted)
        self.selector=ExperienceSelector(self.search,'private-scope',lambda *args:True)

    def submit(self):return apply(self.store,'submit','review.submit',self.submission,self.authorize)
    def approve(self,**kw):
        return apply(self.store,kw.pop('command','approve'),'review.approve',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',draft=self.draft,**kw),self.authorize)
    def publish(self,result,rid='experience-release'):
        with self.store.connection() as db:refs=[(r['id'],r['version']) for r in db.execute("SELECT id,version FROM records WHERE set_id='lessons'")]
        m=self.store.create_release('lessons',rid,'generation-'+rid,'test-space',refs,{'experience':'v1'})
        GenerationBuilder(self.store,self.encoder,self.vector).build(rid)
        self.store.attest_ready(rid,dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space='test-space',record_count=len(refs),watermark=1))
        self.store.apply_command('publish-'+rid,'release.publish',dict(set_id='lessons',release_id=rid,manifest_hash=checksum(m)))
    def select(self,environment='production',stage='check',releases=None,budget=10000):
        facts={'environment':{'value':environment,'evidence':[dict(source='new-document',locator='p2')]}}
        return self.selector.select(releases or ['experience-release'],'журнал срок хранения',stage,facts,['release'],lambda t:len(t)//4,budget)

    def test_explicit_review_curator_and_semantic_transfer(self):
        self.submit();approved=self.approve();self.publish(approved)
        hits=self.select();self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]['source_priority'],'example_only')
        self.assertEqual(self.select('test'),[])
        self.assertEqual(self.select(stage='cross'),[])
        self.assertEqual(self.select(budget=1),[])

    def test_no_automatic_lesson_and_no_unverified_quotes(self):
        self.submit()
        with self.store.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind IN ('review_case','clarification')").fetchone()[0],0)
        bad=copy.deepcopy(self.submission);bad['evidence'][0]['quote']='Выдуманный текст';bad['proposal_id']='bad'
        with self.assertRaises(ValueError):apply(self.store,'bad','review.submit',bad,self.authorize)

    def test_revocation_and_restoration_by_new_version(self):
        self.submit();a=self.approve();self.publish(a)
        payload=dict(set_id='lessons',actor_id='curator',proposal_id='proposal',experience_id=a['record_id'],version=1,reason='Пересмотреть условия')
        apply(self.store,'revoke','review.revoke',payload,self.authorize)
        self.assertEqual(self.select(),[])
        b=self.approve(command='restore',experience_id=a['record_id'],version=2);self.publish(b,'restored-release')
        self.assertEqual(self.select(),[])
        self.assertEqual(self.select(releases=['restored-release'])[0]['version'],2)

    def test_scope_version_unknown_and_access_fail_closed(self):
        self.submit();a=self.approve();self.publish(a)
        self.selector.scope_id='other-project';self.assertEqual(self.select(),[])
        self.selector.scope_id='private-scope'
        self.assertEqual(self.selector.eligible('experience-release','check',{},['release']),{})
        self.assertEqual(self.selector.eligible('experience-release','check',{},['new-normative-release']),{})
        self.granted=False
        with self.assertRaises(PermissionError):self.select()
        with self.assertRaises(PermissionError):self.submit()

    def test_curator_cannot_broaden_authors_fact_conditions(self):
        self.submit()
        broader=copy.deepcopy(self.draft)
        broader['conditions']={'any_of':[self.draft['conditions'],{'fact':{'name':'environment','in':['test']}}]}
        with self.assertRaises(PermissionError):
            apply(self.store,'broad-review','review.approve',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',draft=broader),self.authorize)
        narrower=copy.deepcopy(self.draft)
        narrower['conditions']={'all_of':[self.draft['conditions'],{'fact':{'name':'document_type','in':['OIT']}}]}
        result=apply(self.store,'narrow-review','review.approve',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',draft=narrower),self.authorize)
        self.assertEqual(result['state'],'approved')

    def test_idempotency_and_scope_widening(self):
        self.assertEqual(self.submit(),self.submit())
        bad=copy.deepcopy(self.draft);bad['scope_id']='organization'
        with self.assertRaises(Conflict):apply(self.store,'bad','review.approve',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',draft=bad),self.authorize)
        a=self.approve();self.assertEqual(a,self.approve())

    def test_norm_change_cannot_be_approved_as_lesson(self):
        self.submission['proposal_kind']='norm_change';self.submit()
        with self.assertRaises(NotReady):self.approve()

    def test_revoke_during_vector_query(self):
        self.submit();a=self.approve();self.publish(a)
        original=self.vector.search
        def revoke(*args,**kwargs):
            result=original(*args,**kwargs)
            apply(self.store,'revoke','review.revoke',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',experience_id=a['record_id'],version=1,reason='revoked'),self.authorize)
            return result
        self.vector.search=revoke
        self.assertEqual(self.select(),[])

    def test_new_document_receives_relevant_experience_only(self):
        self.submit();a=self.approve();self.publish(a)
        path=self.fixture.path.parent/'independent.docx'
        docx(path,'Журнал другой системы хранится 720 часов.')
        did=corpus([path])[0]['id']
        facts={did:{'type':{'value':'test','evidence':[dict(source=did,locator='p1')]},
                    'environment':{'value':'production','evidence':[dict(source=did,locator='p2')]}}}
        runner=ReviewRunner(self.store,self.fixture.model,lambda sid:True,experience_selector=self.selector)
        # A concise example fits within 12% of the request; no quoted private
        # document text is included in the published example.
        task=runner.create([path],{did:{'release':['profile']}},facts,lambda *args:True,experience_releases=['experience-release'])
        runner.run_once();report=runner.report(task)
        used=[v for result in report['experience_used'].values() for values in result.values() for v in values]
        self.assertTrue(used)
        self.assertTrue(all(v['id']==a['record_id'] for v in used))
        self.assertTrue(all('evidence' not in v for v in used))
        facts[did]['environment']['value']='test'
        counter=runner.create([path],{did:{'release':['profile']}},facts,lambda *args:True,experience_releases=['experience-release'])
        runner.run_once();report=runner.report(counter)
        self.assertTrue(all(not values for result in report['experience_used'].values() for values in result.values()))

    def test_similarity_does_not_merge_different_conditions(self):
        self.submit();a=self.approve();self.publish(a)
        draft=copy.deepcopy(self.draft);draft['conditions']={'fact':{'name':'environment','in':['laboratory']}}
        suggestions=self.selector.related('experience-release',draft)
        self.assertTrue(suggestions)
        self.assertFalse(suggestions[0]['same_conditions'])
        self.assertEqual(suggestions[0]['decision'],'curator_comparison_required')

    def test_corrected_card_is_new_version_not_changed_source(self):
        from .test_norms import run,block
        fragment=block('3.1','Система должна хранить журнал 30 дней.');fragment['context_hash']='hash'
        card=run(fragment)['cards'][0]
        rid=card['id']
        original=dict(fragment_refs=[['f',1]],source_revision=['source',1],modality=card['modality'],condition=card['applicability'],card=card)
        self.store.put_record('set','requirement',rid,1,original)
        with self.store.connection() as db:refs=[(r['id'],r['version']) for r in db.execute("SELECT id,version FROM records WHERE set_id='set'")]
        m=self.store.create_release('set','repair-norm','repair-generation','test-space',refs,{'test':'1'})
        self.store.attest_ready('repair-norm',dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space='test-space',record_count=len(refs),watermark=1))
        self.store.apply_command('repair-publish','release.publish',dict(set_id='set',release_id='repair-norm',manifest_hash=checksum(m)))
        target=dict(set_id='set',release_id='repair-norm',requirement_ref=[rid,1])
        self.store.put_record('lessons','review_proposal','repair-proposal',1,dict(portal_review_id='repair-proposal',scope_id='private-scope',evidence=[],proposal_kind='extraction_error',draft={'normative_refs':[target]}))
        data=dict(set_id='lessons',actor_id='curator',proposal_id='repair-proposal',normative_ref=target,corrected_card=card)
        bad=copy.deepcopy(data);bad['corrected_card']['modality']='permitted'
        with self.assertRaises(ValueError):apply(self.store,'bad-repair','review.repair',bad,self.authorize)
        result=apply(self.store,'repair','review.repair',data,self.authorize)
        self.assertEqual(result['state'],'corrected_draft');self.assertEqual(result['version'],2)
        with self.store.connection() as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=1',(rid,)).fetchone()[0]),original)
            self.assertEqual(json.loads(db.execute("SELECT payload FROM records WHERE id='source' AND version=1").fetchone()[0])['sha256'],'a'*64)

    def test_conflicting_active_experience_requires_resolution(self):
        self.submit();a=self.approve()
        self.draft=copy.deepcopy(self.draft);self.draft['conflicts_with']=[[a['record_id'],1]]
        with self.assertRaises(NotReady):self.approve(command='conflict',experience_id=a['record_id'],version=2)

    def test_explicit_publisher_and_rejected_proposal(self):
        from knowledge_v2.experience import publish
        self.submit();a=self.approve()
        result=publish(self.store,'auto-publish',dict(set_id='lessons',actor_id='curator'),self.encoder,self.vector,self.authorize)
        self.assertEqual(len(self.select(releases=[result['release_id']])),1)
        self.assertEqual(result,publish(self.store,'auto-publish',dict(set_id='lessons',actor_id='curator'),self.encoder,self.vector,self.authorize))

    def test_model_suggestion_does_not_activate_knowledge(self):
        from knowledge_v2.experience import suggest
        self.submit()
        class Advisor:
            context=8192;output_tokens=1024;signature='test-advisor'
            def count(self,p):return 1000
            def complete(self,p):return {k:self_outer.draft[k] for k in ('summary','counterexample','required_evidence')}
        self_outer=self
        result=suggest(self.store,'suggest',dict(set_id='lessons',actor_id='local',proposal_id='proposal'),Advisor(),self.authorize)
        self.assertFalse(result['expert_validation']);self.assertEqual(result['state'],'pending')
        with self.store.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind IN ('review_case','clarification')").fetchone()[0],0)

    def test_reject_cannot_leave_active_lesson(self):
        self.submit();self.approve()
        with self.assertRaises(Conflict):
            apply(self.store,'reject','review.reject',dict(set_id='lessons',actor_id='curator',proposal_id='proposal',reason='Reject'),self.authorize)

    def test_model_turn_serializes_and_releases_after_error(self):
        import threading,time
        from knowledge_v2.model_queue import model_turn
        entered=threading.Event();release=threading.Event();second=threading.Event();errors=[]
        client=type('Client',(),{'signature':'same-model','timeout':30})()
        def first():
            try:
                with model_turn(self.store,client):entered.set();release.wait(3);raise RuntimeError('inference failure')
            except RuntimeError:pass
            except Exception as exc:errors.append(exc)
        def next_turn():
            try:
                with model_turn(self.store,client):second.set()
            except Exception as exc:errors.append(exc)
        a=threading.Thread(target=first);b=threading.Thread(target=next_turn)
        a.start();self.assertTrue(entered.wait(2));b.start()
        try:self.assertFalse(second.wait(.3))
        finally:release.set();a.join(4);b.join(4)
        self.assertFalse(errors);self.assertTrue(second.is_set())
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM model_tickets').fetchone()[0],0)


if __name__=='__main__':unittest.main()
