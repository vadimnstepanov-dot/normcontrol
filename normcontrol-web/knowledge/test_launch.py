import os,uuid
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from portal.models import Batch,Document,WorkerRun
from .models import Scope,NormativeSet,Release,Command,KnowledgeCheck
from .launch import start,reconcile,LaunchForm
from .checks import control
from . import services


@override_settings(KNOWLEDGE_V2_ENABLED=True)
class UnifiedLaunchTests(TestCase):
    def setUp(self):
        self.client.defaults['HTTP_SEC_FETCH_DEST']='iframe'
        self.user=User.objects.create_user('launch-owner')
        self.scope=Scope.objects.create(owner=self.user,name='Shared norms',kind='project')
        self.rules=NormativeSet.objects.create(scope=self.scope,name='Normative base',created_by=self.user,state='ready')
        self.release=Release.objects.create(id=uuid.uuid4(),normative_set=self.rules,state='active',manifest_hash='a'*64,attestation={},
            manifest={'generation_id':'generation-test','embedding_space':'test','versions':{'curation_digest':'test'},'items':[]})
        self.rules.active_release=self.release;self.rules.save()
        self.batch=Batch.objects.create(owner=self.user,name='Launch package',checks=['sto','logic','language'])
        Document.objects.create(batch=self.batch,name='test.docx',file='test.docx',size=10,sha256='b'*64)
        self.client.force_login(self.user)
        self.token='test-only-worker-launch-'+('x'*40)
        self.env=patch.dict(os.environ,{'NORMCONTROL_WORKER_TOKEN':self.token});self.env.start();self.addCleanup(self.env.stop)

    def launch(self,directions=None,key=None):
        return start(self.user,self.batch.pk,directions or ['sto','logic','language'],[str(self.rules.pk)],None,key or uuid.uuid4())

    def claim(self):return services.claim('test-v2',['review.execute','trace.suggest'],['context-budget-v5','visual-tail-v1'])

    def test_pipeline_feature_accepted_at_authenticated_http_boundary(self):
        import json
        with override_settings(KNOWLEDGE_WORKER_TOKEN=self.token),patch('knowledge.services.claim',return_value=None) as claim:
            for feature,status in [('pipeline-v1',200),('unknown-feature',400)]:
                response=self.client.post('/normcontol/api/v2/worker/claim/',
                    data=json.dumps({'protocol_version':2,'capabilities':['review.execute'],'features':[feature]}),
                    content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
                self.assertEqual(response.status_code,status)
            claim.assert_called_once()

    def test_pipeline_status_includes_live_normative_stage(self):
        job=self.launch()
        WorkerRun.objects.create(batch=self.batch,state='running',local_id='active-native',
                                 snapshot={'pipeline':[{'stage':'material','state':'done'}]})
        response=self.client.get(f'/normcontol/batches/{self.batch.pk}/status/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['snapshot']['pipeline_normative']['state'],job.state)

    def test_pipeline_claim_can_prepare_while_native_is_running(self):
        from portal.models import WorkerPresence
        WorkerPresence.objects.create(name='new-native',state='idle',details={'pipeline_version':'pipeline-v1'})
        job=self.launch();WorkerRun.objects.create(batch=self.batch,state='running',worker='new-native',local_id='native-preparing')
        c=Command.objects.get(payload__job_id=str(job.pk));self.assertEqual(c.payload['pipeline_version'],'pipeline-v1')
        self.assertIsNone(self.claim());c.refresh_from_db();self.assertEqual(c.attempts,0)
        claim=services.claim('unified',['review.execute','trace.suggest'],['context-budget-v5','visual-tail-v1','pipeline-v1'])
        self.assertIsNotNone(claim)

    def test_pipeline_queued_package_does_not_block_previous_normative_retry(self):
        from portal.models import WorkerPresence
        WorkerPresence.objects.create(name='new-native',state='idle',details={'pipeline_version':'pipeline-v1'})
        newer=self.launch()
        ready=Batch.objects.create(owner=self.user,name='Earlier package',checks=['sto','logic'])
        Document.objects.create(batch=ready,name='earlier.docx',file='earlier.docx',size=10,sha256='c'*64)
        job=start(self.user,ready.pk,['sto','logic'],[str(self.rules.pk)],None,uuid.uuid4())
        WorkerRun.objects.create(batch=ready,state='running',worker='new-native',local_id='ready-native')
        claim=services.claim('unified',['review.execute','trace.suggest'],['context-budget-v5','visual-tail-v1','pipeline-v1'])
        self.assertEqual(claim['payload']['job_id'],str(job.pk))
        blocked=Command.objects.get(payload__job_id=str(newer.pk))
        self.assertEqual(blocked.state,'pending');self.assertEqual(blocked.attempts,0)

    def test_pipeline_claimed_native_without_local_job_keeps_normative_pending(self):
        from portal.models import WorkerPresence
        WorkerPresence.objects.create(name='new-native',state='idle',details={'pipeline_version':'pipeline-v1'})
        job=self.launch();WorkerRun.objects.create(batch=self.batch,state='claimed',worker='new-native')
        self.assertIsNone(services.claim('unified',['review.execute','trace.suggest'],['context-budget-v5','visual-tail-v1','pipeline-v1']))
        command=Command.objects.get(payload__job_id=str(job.pk));self.assertEqual(command.attempts,0)

    def test_legacy_launch_still_waits_for_native(self):
        from .launch import dependency_ready
        job=self.launch();WorkerRun.objects.create(batch=self.batch,state='running',worker='legacy')
        c=Command.objects.get(payload__job_id=str(job.pk));self.assertNotIn('pipeline_version',c.payload)
        self.assertFalse(dependency_ready(c));c.refresh_from_db();self.assertEqual(c.attempts,0)

    def test_stale_native_presence_does_not_enable_pipeline(self):
        from portal.models import WorkerPresence
        from django.utils import timezone
        from datetime import timedelta
        WorkerPresence.objects.create(name='stale',state='idle',details={'pipeline_version':'pipeline-v1'})
        WorkerPresence.objects.filter(pk='stale').update(heartbeat=timezone.now()-timedelta(minutes=5))
        job=self.launch();self.assertNotIn('pipeline_version',Command.objects.get(payload__job_id=str(job.pk)).payload)

    def test_planning_version_routes_new_jobs_and_preserves_legacy_delivery(self):
        job=self.launch(['sto']);c=Command.objects.get(payload__job_id=str(job.pk))
        self.assertIsNone(services.claim('old',['review.execute','trace.suggest']))
        self.assertIsNone(services.claim('v4',['review.execute','trace.suggest'],['context-budget-v4','visual-tail-v1']))
        c.refresh_from_db();self.assertEqual(c.attempts,0)
        self.assertIsNotNone(self.claim())

    def test_new_worker_cannot_take_old_pinned_command(self):
        job=self.launch(['sto']);c=Command.objects.get(payload__job_id=str(job.pk))
        c.payload.pop('planning_version');c.payload.pop('visual_version',None);c.save(update_fields=['payload'])
        self.assertIsNone(self.claim());c.refresh_from_db();self.assertEqual(c.attempts,0)
        self.assertIsNotNone(services.claim('old',['review.execute','trace.suggest']))

    def test_profile_unaware_worker_cannot_claim_new_visual_plan(self):
        job=self.launch(['sto']);c=Command.objects.get(payload__job_id=str(job.pk))
        self.assertIsNone(services.claim('text-only',['review.execute'],['context-budget-v4']))
        c.refresh_from_db();self.assertEqual(c.attempts,0)
        self.assertIsNotNone(self.claim())

    def test_claim_shows_preparation_without_fabricating_progress(self):
        job=self.launch(['sto']);original=dict(job.progress)
        self.assertIsNone(services.claim('unaware',['review.execute'],['context-budget-v4']))
        job.refresh_from_db();self.assertEqual(job.state,'queued')
        claim=self.claim();self.assertIsNotNone(claim)
        job.refresh_from_db();self.assertEqual(job.state,'running');self.assertEqual(job.progress,original)
        self.assertIsNone(job.progress['total'])

    def test_v3_running_check_blocks_v4_and_preserves_its_payload(self):
        job=self.launch(['sto']);c=Command.objects.get(payload__job_id=str(job.pk))
        c.payload['planning_version']='context-budget-v3';c.payload.pop('visual_version',None)
        c.save(update_fields=['payload']);original=dict(c.payload)
        self.assertIsNotNone(services.claim('v3',['review.execute','trace.suggest'],['context-budget-v3']))
        second=Batch.objects.create(owner=self.user,name='Next package',checks=['sto'])
        Document.objects.create(batch=second,name='second.docx',file='second.docx',size=10,sha256='c'*64)
        start(self.user,second.pk,['sto'],[str(self.rules.pk)],None,uuid.uuid4())
        self.assertIsNone(self.claim())
        c.refresh_from_db();self.assertEqual(c.payload,original);self.assertEqual(c.state,'delivering')

    def test_v5_never_claims_or_replans_saved_v4_job(self):
        job=self.launch(['sto']);c=Command.objects.get(payload__job_id=str(job.pk))
        c.payload['planning_version']='context-budget-v4';c.save(update_fields=['payload'])
        before=dict(c.payload)
        self.assertIsNone(self.claim())
        self.assertIsNotNone(services.claim('v4',['review.execute','trace.suggest'],['context-budget-v4','visual-tail-v1']))
        c.refresh_from_db();self.assertEqual(c.payload,before)

    def legacy_claim(self):
        return self.client.post('/normcontol/worker/claim/',{'worker':'desktop'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token).json()

    def test_joint_launch_pins_release_filters_legacy_and_waits_without_attempts(self):
        job=self.launch();c=Command.objects.get(payload__job_id=str(job.pk))
        self.assertEqual(c.digest,services.digest(dict(kind=c.kind,set_id=str(c.normative_set_id),actor=self.user.pk,payload=c.payload)))
        self.batch.refresh_from_db();self.assertEqual(self.batch.status,'waiting');self.assertIn('sto',self.batch.checks)
        self.assertIsNone(self.claim());c.refresh_from_db();self.assertEqual(c.attempts,0)
        legacy=self.legacy_claim();self.assertEqual(legacy['checks'],['logic','language'])
        run=WorkerRun.objects.get(batch=self.batch);run.state='running';run.sequence=12;run.local_id='saved';run.save()
        self.assertIsNone(self.claim())
        run.state='completed';run.save();reconcile(self.batch)
        self.assertIsNone(self.legacy_claim()['job'])  # priority for the ready STO stage
        delivery=self.claim();self.assertEqual(delivery['payload']['job_id'],str(job.pk))
        self.assertEqual(job.snapshot.data['releases'][0]['release_id'],str(self.release.pk))
        job.state='partial';job.save();reconcile(self.batch);self.batch.refresh_from_db();self.assertEqual(self.batch.status,'partial')

    def test_sto_only_never_queues_an_empty_legacy_review(self):
        job=self.launch(['sto']);self.assertIsNone(self.legacy_claim()['job']);self.assertIsNotNone(self.claim())
        self.assertFalse(WorkerRun.objects.filter(batch=self.batch).exists())

    def test_no_norms_rolls_back_entire_launch(self):
        with self.assertRaises(ValueError):start(self.user,self.batch.pk,['sto','logic'],[],None,uuid.uuid4())
        self.batch.refresh_from_db();self.assertEqual(self.batch.status,'prepared');self.assertFalse(KnowledgeCheck.objects.exists())

    def test_acl_and_revoked_release_prevent_launch(self):
        self.scope.owner=User.objects.create_user('not-owner');self.scope.save()
        with self.assertRaises(PermissionDenied):self.launch()
        self.assertFalse(KnowledgeCheck.objects.exists());self.assertFalse(WorkerRun.objects.exists())

    def test_revoked_scope_while_waiting_fails_without_consuming_attempt(self):
        job=self.launch();self.scope.owner=User.objects.create_user('scope-revoked');self.scope.save()
        self.assertIsNone(self.claim());job.refresh_from_db();self.assertEqual(job.state,'failed')
        self.assertEqual(Command.objects.get(payload__job_id=str(job.pk)).attempts,0)

    def test_add_sto_keeps_running_job_and_original_sequence(self):
        self.batch.checks=['logic','language'];self.batch.status='running';self.batch.save()
        run=WorkerRun.objects.create(batch=self.batch,state='running',sequence=24,local_id='already-running',snapshot={'saved':True})
        self.launch();run.refresh_from_db()
        self.assertEqual((run.sequence,run.local_id,run.snapshot),(24,'already-running',{'saved':True}))
        self.assertIsNone(self.claim())
        with self.assertRaises(services.Conflict):self.launch(['sto','language'])

    def test_repeated_submit_has_one_snapshot_and_command(self):
        key=uuid.uuid4();first=self.launch(key=key);second=self.launch(key=key)
        self.assertEqual(first.pk,second.pk);self.assertEqual(KnowledgeCheck.objects.count(),1);self.assertEqual(Command.objects.count(),1)
        with self.assertRaises(services.Conflict):self.launch(['sto'],key=key)

    def test_pause_before_execution_can_resume_without_duplicate_delivery(self):
        job=self.launch(['sto']);control(self.user,job.pk,'pause');self.assertIsNone(self.claim())
        control(self.user,job.pk,'resume');delivery=self.claim();self.assertIsNotNone(delivery)
        self.assertIsNone(self.claim());self.assertEqual(Command.objects.filter(state='delivering').count(),1)

    def test_legacy_failure_does_not_claim_false_completion(self):
        job=self.launch();WorkerRun.objects.create(batch=self.batch,state='failed')
        self.assertIsNone(self.claim());job.refresh_from_db();self.assertEqual(job.state,'failed')
        self.batch.refresh_from_db();self.assertEqual(self.batch.status,'failed')

    def test_other_active_legacy_job_blocks_model_overlap(self):
        self.launch(['sto']);other=Batch.objects.create(owner=self.user,name='Busy',status='running')
        WorkerRun.objects.create(batch=other,state='running');self.assertIsNone(self.claim())

    def test_launch_ui_post_and_queue_redirect(self):
        response=self.client.post(f'/normcontol/batches/{self.batch.pk}/action/',{'action':'queue'})
        self.assertRedirects(response,f'/normcontol/knowledge/check/{self.batch.pk}/')
        page=self.client.get(response.url);self.assertContains(page,'Соответствие СТО');self.assertContains(page,'Запустить проверку')
        form=page.context['form']
        invalid=self.client.post(response.url,{'checks':['sto','logic'],'launch_key':str(form.initial['launch_key'])})
        self.assertContains(invalid,'выберите хотя бы один опубликованный');self.assertFalse(KnowledgeCheck.objects.exists())
        good=self.client.post(response.url,{'checks':['sto','logic'],'normative_sets':[str(self.rules.pk)],'launch_key':str(form.initial['launch_key'])})
        self.assertRedirects(good,f'/normcontol/?batch={self.batch.pk}#current-review')
        self.assertContains(self.client.get(good.url),'Соответствие СТО · в очереди')

    def test_repeat_preserves_previous_norm_selection(self):
        job=self.launch();job.state='completed';job.save();self.batch.status='completed';self.batch.save()
        copy=Batch.objects.create(owner=self.user,name='Repeat',source_batch=self.batch,checks=['sto','logic'])
        form=LaunchForm(user=self.user,batch=copy)
        self.assertEqual(form.initial['normative_sets'],[str(self.rules.pk)])

    def test_dashboard_keeps_actual_legacy_progress_with_pending_sto(self):
        job=self.launch();WorkerRun.objects.create(batch=self.batch,state='running',snapshot={'stages':{'language':{'done':2,'pending':3}}})
        self.batch.status='running';self.batch.save()
        page=self.client.get('/normcontol/',{'batch':str(self.batch.pk)})
        self.assertIsNone(page.context['selected_knowledge']);self.assertEqual(page.context['pending_knowledge'].pk,job.pk)
        self.assertContains(page,'id="review-progress"');self.assertContains(page,'Соответствие СТО · в очереди')

    def test_native_completion_keeps_package_active_until_sto_finishes(self):
        job=self.launch();run=WorkerRun.objects.create(batch=self.batch,state='running',worker='desktop')
        response=self.client.post(f'/normcontol/worker/{run.lease}/update/',
            {'sequence':1,'state':'completed','local_id':'local-check','snapshot':{}},
            content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(response.status_code,200);self.batch.refresh_from_db()
        self.assertEqual(self.batch.status,'running');run.refresh_from_db();self.assertEqual(run.state,'completed')
        job.state='completed';job.save();reconcile(self.batch);self.batch.refresh_from_db();self.assertEqual(self.batch.status,'completed')

    def test_rerun_v2_waits_for_selection_and_duplicate_click_reuses_revision(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        import tempfile
        with tempfile.TemporaryDirectory() as temporary,override_settings(MEDIA_ROOT=temporary):
            doc=self.batch.documents.get();doc.file.save('test.docx',SimpleUploadedFile('test.docx',b'test'));doc.save()
            self.batch.status='completed';self.batch.save()
            first=self.client.post(f'/normcontol/batches/{self.batch.pk}/action/',{'action':'rerun'})
            repeat=self.batch.reruns.get();self.assertEqual(repeat.status,'prepared')
            second=self.client.post(f'/normcontol/batches/{self.batch.pk}/action/',{'action':'rerun'})
            self.assertEqual(first.url,second.url);self.assertEqual(self.batch.reruns.count(),1)
