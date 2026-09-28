import copy
import json
import uuid
from pathlib import Path
from django.test import TestCase, Client, override_settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from datetime import timedelta
from .test_uploads import UploadTests, TOKEN
from .models import Command, SourceUpload, DocumentProfile
from . import services as s
from .profiles import save_profile, snapshot_profiles
from knowledge_v2.tests.test_ingest import docx


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class NormativeTests(TestCase):
    def setUp(self):
        UploadTests.setUp(self)
        from knowledge_v2.tests.test_semantic import Model
        self.bridge.analysis_client=Model()
    transport=UploadTests.transport
    download=UploadTests.download
    upload=UploadTests.upload

    def prepared(self):
        r=self.upload(docx(Path(self.tmp.name)/'rule.docx').read_bytes())
        sid=r.json()['sources'][0]['id'];self.bridge.once()
        return sid

    def test_user_prepares_and_publishes_real_indexed_release(self):
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        sid=self.prepared();base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/'
        self.assertContains(self.client.get('/normcontol/knowledge/'),'Нормативная база ещё не создана')
        analyzed=self.client.post(base+f'sources/{sid}/analysis/',data='{}',content_type='application/json',HTTP_IDEMPOTENCY_KEY='analysis-ready')
        self.assertEqual(analyzed.status_code,202,analyzed.content);self.assertTrue(self.bridge.once())
        preview=self.client.get(base+f'sources/{sid}/analysis/').json()['entries']
        self.assertTrue(preview);self.assertTrue(preview[0]['citations'])
        body={'source_ids':[sid],'expected_revision':1}
        response=self.client.post(base+'prepare/',data=json.dumps(body),content_type='application/json',HTTP_IDEMPOTENCY_KEY='prepare-one')
        self.assertEqual(response.status_code,202,response.content)
        self.assertTrue(self.bridge.once())
        detail=self.client.get(base).json();ready=detail['releases'][0]
        self.assertEqual(ready['state'],'ready');self.assertGreater(ready['requirement_count'],0)
        response=self.client.post(base+'publish/',data=json.dumps({'release_id':ready['id'],'expected_revision':1}),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='publish-one')
        self.assertEqual(response.status_code,202,response.content);self.assertTrue(self.bridge.once())
        self.assertEqual(self.client.get(base).json()['active_release'],ready['id'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(base).status_code,403)

    def test_experience_and_normative_releases_cannot_replace_each_other(self):
        experience=s.create_set(self.user,'Lessons',self.scope.pk,'lesson-set',purpose='experience')
        self.assertEqual(self.client.post(f'/normcontol/api/v2/normative-sets/{experience.pk}/prepare/',
            data=json.dumps({'source_ids':['x'],'expected_revision':1}),content_type='application/json',HTTP_IDEMPOTENCY_KEY='wrong').status_code,422)
        self.assertEqual(self.client.post(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/experience/publish/',
            data=json.dumps({'expected_revision':1}),content_type='application/json',HTTP_IDEMPOTENCY_KEY='wrong-experience').status_code,422)

    def test_archive_restore_and_revoke_normative_release(self):
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        from .models import NormativeSet,Release
        self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        sid=self.prepared();base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/'
        s.analyze_source(self.user,self.dataset.pk,sid,'lifecycle-analysis');self.bridge.once()
        s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'lifecycle-prepare');self.bridge.once()
        rid=self.client.get(base).json()['releases'][0]['id']
        s.publish(self.user,self.dataset.pk,rid,1,'lifecycle-publish');self.bridge.once()
        current=Release.objects.get(pk=rid);revised=copy.deepcopy(current.manifest)
        revised['items'].append(dict(id=str(uuid.uuid4()),version=1,kind='fragment',digest='a'*64))
        next_release=Release.objects.create(id=uuid.uuid4(),normative_set=self.dataset,manifest=revised,
            manifest_hash='a'*64,attestation={},state='ready')
        comparison=self.client.get(base+f'compare/?old={rid}&new={next_release.pk}')
        self.assertEqual(comparison.status_code,200,comparison.content)
        self.assertEqual(comparison.json()['added']['count'],1)
        archived=self.client.post(base+'state/',data=json.dumps({'action':'archive','expected_revision':2}),content_type='application/json')
        self.assertEqual(archived.status_code,200,archived.content)
        self.assertEqual(archived.json()['state'],'archived')
        self.assertEqual(s.change_set_state(self.user,self.dataset.pk,'restore',3).state,'ready')
        revoked=self.client.post(base+'revoke/',data=json.dumps({'release_id':rid,'reason':'Утрачена нормативная актуальность','expected_revision':4}),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='lifecycle-revoke')
        self.assertEqual(revoked.status_code,202,revoked.content)
        self.assertTrue(self.bridge.once())
        dataset=NormativeSet.objects.get(pk=self.dataset.pk)
        self.assertEqual(dataset.state,'revoked');self.assertIsNone(dataset.active_release_id)
        self.assertEqual(Release.objects.get(pk=rid).state,'revoked')
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(base+'state/',data=json.dumps({'action':'restore','expected_revision':5}),content_type='application/json').status_code,403)

    def test_portal_batch_uses_selected_v2_release_and_local_review(self):
        import hashlib,contextlib,io
        from django.core.files.base import ContentFile
        from portal.models import Batch,Document,WorkerRun
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        from knowledge_v2.tests.test_review import Model
        self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        sid=self.prepared();base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/'
        self.client.post(base+f'sources/{sid}/analysis/',data='{}',content_type='application/json',HTTP_IDEMPOTENCY_KEY='analysis-check')
        self.bridge.once()
        self.client.post(base+'prepare/',data=json.dumps({'source_ids':[sid],'expected_revision':1}),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='prepare-check')
        self.bridge.once();ready=self.client.get(base).json()['releases'][0]
        self.client.post(base+'publish/',data=json.dumps({'release_id':ready['id'],'expected_revision':1}),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='publish-check')
        self.bridge.once()
        batch=Batch.objects.create(owner=self.user,name='Independent check',status='prepared',checks=['sto'])
        contents=docx(Path(self.tmp.name)/'target.docx','Система должна хранить журнал 2 дня.').read_bytes()
        document=Document(batch=batch,name='target.docx',sha256=hashlib.sha256(contents).hexdigest(),size=len(contents))
        document.file.save('target.docx',ContentFile(contents),save=True)
        self.bridge.check_downloader=lambda command_id,job_id,document_id,lease:contextlib.closing(io.BytesIO(contents))
        self.bridge.check_client=Model()
        response=self.client.post('/normcontol/api/v2/checks/',data=json.dumps({'batch_id':str(batch.pk),'set_ids':[str(self.dataset.pk)]}),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='check-one')
        self.assertEqual(response.status_code,202,response.content)
        job_id=response.json()['id'];self.assertTrue(self.bridge.once())
        result=self.client.get(f'/normcontol/api/v2/checks/{job_id}/').json()
        self.assertIn(result['state'],('completed','partial'))
        self.assertEqual(result['releases'][0]['release_id'],ready['id'])
        self.assertGreater(result['progress']['total'],0)
        findings=self.client.get(f'/normcontol/api/v2/checks/{job_id}/findings/').json()['entries']
        self.assertTrue(findings)
        for extension in ('xlsx','docx'):
            exported=self.client.get(f'/normcontol/knowledge/checks/{job_id}/?format={extension}')
            self.assertEqual(exported.status_code,200,exported.content[:300])
            self.assertTrue(exported.content.startswith(b'PK'))
        lesson=s.create_set(self.user,'Reviewed examples',self.scope.pk,'check-lesson-set',purpose='experience')
        self.bridge.once()
        first=findings[0];basis=first['obligation']
        proof=next(e for part in first['partition_decisions'] for e in part['evidence'] if e.get('block_id') and e.get('quote'))
        predicate={'fact':{'name':'selected_sources','in':[basis['source_revision'][0]]}}
        submission=dict(task_id=result['summary']['task_id'],obligation_id=basis['id'],proposal_kind='private_example',
            comment='Проверить срок хранения и подтверждающие доказательства.',evidence=[{'block_id':proof['block_id'],'quote':proof['quote']}],
            draft=dict(summary='Сопоставлять установленный нормативом срок с требованиями проекта.',conditions=predicate,
                counter_conditions={'not':predicate},counterexample='Иной нормативный источник не наследует данный срок.',
                required_evidence='Точная цитата нормы и срока проекта.',stages=['check','verify'],scope_id=str(self.scope.pk),
                normative_refs=[dict(set_id=str(self.dataset.pk),release_id=ready['id'],requirement_ref=basis['requirement_ref'])],
                sharing_confirmed=True))
        reviewed=self.client.post(f'/normcontol/api/v2/normative-sets/{lesson.pk}/reviews/',data=json.dumps(submission),
            content_type='application/json',HTTP_IDEMPOTENCY_KEY='check-reviewed')
        self.assertEqual(reviewed.status_code,202,reviewed.content)
        self.bridge.once()
        self.assertEqual(self.client.get(f"/normcontol/api/v2/reviews/{reviewed.json()['id']}/").json()['state'],'pending')
        self.assertEqual(WorkerRun.objects.filter(batch=batch).count(),0)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/normcontol/api/v2/checks/{job_id}/').status_code,403)

    def test_v2_check_pauses_and_resumes_from_committed_partition(self):
        import hashlib,contextlib,io
        from django.core.files.base import ContentFile
        from portal.models import Batch,Document
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        from knowledge_v2.tests.test_review import Model
        from . import checks
        self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        sid=self.prepared();base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/'
        s.analyze_source(self.user,self.dataset.pk,sid,'pause-analysis');self.bridge.once()
        s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'pause-prepare');self.bridge.once()
        ready=self.client.get(base).json()['releases'][0]
        s.publish(self.user,self.dataset.pk,ready['id'],1,'pause-publish');self.bridge.once()
        batch=Batch.objects.create(owner=self.user,name='Pause test',status='prepared',checks=['logic'])
        data={}
        for number in (1,2):
            contents=docx(Path(self.tmp.name)/f'target-{number}.docx',f'Система хранит журнал {number} дня.').read_bytes()
            row=Document(batch=batch,name=f'target-{number}.docx',sha256=hashlib.sha256(contents).hexdigest(),size=len(contents))
            row.file.save(row.name,ContentFile(contents),save=True);data[row.pk]=contents
        self.bridge.check_downloader=lambda command_id,job_id,document_id,lease:contextlib.closing(io.BytesIO(data[document_id]))
        model=Model();self.bridge.check_client=model
        job,_=checks.start(self.user,batch.pk,[str(self.dataset.pk)],None,'pause-job')
        transport=self.bridge.transport;requested=False
        def pause_after_first(path,payload):
            nonlocal requested
            if path=='/worker/checks/progress/' and payload['progress']['completed']>=1 and not requested:
                requested=True;checks.control(self.user,job.pk,'pause')
            return transport(path,payload)
        self.bridge.transport=pause_after_first
        self.assertTrue(self.bridge.once())
        job.refresh_from_db();self.assertEqual(job.state,'paused');self.assertGreater(job.progress['completed'],0)
        finished=job.progress['completed'];calls=len(model.calls)
        self.bridge.transport=transport;checks.control(self.user,job.pk,'resume')
        self.assertTrue(self.bridge.once())
        job.refresh_from_db();self.assertIn(job.state,('completed','partial'))
        self.assertGreater(job.progress['completed'],finished)
        self.assertGreater(len(model.calls),calls)

    def test_analysis_worker_api_replay_and_permissions(self):
        sid=self.prepared();url=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{sid}/analysis/'
        r=self.client.post(url,data='{}',content_type='application/json',HTTP_IDEMPOTENCY_KEY='analysis-one')
        self.assertEqual(r.status_code,202,r.content);cid=r.json()['command_id']
        self.assertTrue(self.bridge.once())
        r=self.client.get(url);self.assertEqual(r.status_code,200,r.content)
        self.assertEqual(r.json()['state'],'done');self.assertIsNone(r.json()['result']['summary']['violation_count'])
        self.assertTrue(r.json()['entries'])
        self.assertEqual(r.json()['result']['summary']['extractor_version'],'semantic-9.1.3')
        self.assertGreater(self.bridge.analysis_client.calls,0)
        self.assertTrue(DocumentProfile.objects.filter(scope=self.scope).exists())
        self.assertTrue(all(e['expert_status']=='unreviewed' for e in r.json()['entries']))
        again=self.client.post(url,data='{}',content_type='application/json',HTTP_IDEMPOTENCY_KEY='analysis-one')
        self.assertEqual(again.json()['command_id'],cid)
        self.assertFalse(self.bridge.once())
        self.client.force_login(self.other);self.assertEqual(self.client.get(url).status_code,403)

    def test_lost_analysis_response_and_expired_lease_replay(self):
        sid=self.prepared();c=s.analyze_source(self.user,self.dataset.pk,sid,'retry')
        normal=self.bridge.transport
        def failing(path,payload):
            if path=='/worker/events/' and payload['payload'].get('kind')=='source.analyzed':raise OSError('Lost network')
            return normal(path,payload)
        self.bridge.transport=failing
        with self.assertRaises(OSError):self.bridge.once()
        count=self.store.counts();self.bridge.transport=normal
        self.assertTrue(self.bridge.once());self.assertEqual(count,self.store.counts())
        c.refresh_from_db();self.assertEqual(c.state,'done')

    def test_failure_of_analysis_keeps_ingestion_and_enforces_lease(self):
        sid=self.prepared();s.analyze_source(self.user,self.dataset.pk,sid,'failure')
        claim=s.claim('test-worker',['source.analyze'])
        s.fail_source('test-worker',claim['command_id'],claim['lease'],'processing_failed',True)
        self.assertEqual(SourceUpload.objects.get(pk=sid).state,'prepared')
        entries=[{'id':'x','locator':'p1','state':'needs_review','validation':{}}]
        with self.assertRaises(s.Conflict):
            s.add_analysis('test-worker',claim['command_id'],claim['lease'],0,entries,s.digest(entries))

    def test_incomplete_projection_cannot_complete(self):
        sid=self.prepared();s.analyze_source(self.user,self.dataset.pk,sid,'partial')
        claim=s.claim('test-worker',['source.analyze'])
        from knowledge_v2.norm_runtime import analyze_source
        payload=analyze_source(self.store,str(self.dataset.pk),sid,client=self.bridge.analysis_client)['summary']
        import uuid
        with self.assertRaises(s.NotReady):
            s.accept_event('test-worker',uuid.uuid4(),claim['command_id'],claim['lease'],payload,s.digest(payload))

    def definition(self,name,parents=()):
        return dict(name=name,parents=list(parents),bindings=[],description='Область применения по утверждённой политике',
                    expression={'fact':{'name':'organization','in':['Demo organization']}})

    def test_admin_profiles_versioned_multilayer_and_cycle_protection(self):
        with self.assertRaises(PermissionDenied):save_profile(self.user,self.scope.pk,self.definition('Organization'))
        self.user.is_staff=True;self.user.save()
        org=save_profile(self.user,self.scope.pk,self.definition('Organization'))
        standard=save_profile(self.user,self.scope.pk,self.definition('Standard',[str(org.pk)]))
        oit=save_profile(self.user,self.scope.pk,self.definition('OIT',[str(standard.pk)]))
        frozen=copy.deepcopy(snapshot_profiles(self.user,[str(org.pk),str(standard.pk),str(oit.pk)]))
        self.assertEqual(len(frozen),3)
        changed=self.definition('New name');save_profile(self.user,self.scope.pk,changed,org.pk,1)
        self.assertEqual(next(x for x in frozen if x['id']==str(org.pk))['revision'],1)
        self.assertEqual(DocumentProfile.objects.get(pk=org.pk).revisions.count(),2)
        with self.assertRaises(s.Conflict):save_profile(self.user,self.scope.pk,changed,org.pk,1)
        with self.assertRaises(ValueError):save_profile(self.user,self.scope.pk,self.definition('Cycle',[str(oit.pk)]),org.pk,2)

    def test_profile_api_forbids_scripts_and_nonadmins(self):
        body={'scope_id':str(self.scope.pk),'definition':self.definition('Type')}
        url='/normcontol/api/v2/profiles/'
        self.assertEqual(self.client.post(url,data=json.dumps(body),content_type='application/json').status_code,403)
        self.user.is_staff=True;self.user.save()
        body['definition']['expression']={'eval':'__import__("os")'}
        self.assertEqual(self.client.post(url,data=json.dumps(body),content_type='application/json').status_code,400)
        body['definition']=self.definition('Type')
        r=self.client.post(url,data=json.dumps(body),content_type='application/json');self.assertEqual(r.status_code,201,r.content)
        self.client.force_login(self.other);self.assertEqual(self.client.get(url).json()['profiles'],[])

    def test_profile_editor_admin_only_and_create(self):
        url='/normcontol/knowledge/profiles/?scope='+str(self.scope.pk)
        self.assertEqual(self.client.get(url).status_code,403)
        self.user.is_staff=True;self.user.save()
        self.assertContains(self.client.get(url),'Профили документов')
        r=self.client.post(url,{'name':'Документ организации','description':'Корпоративная политика',
                               'organization':'Учебная организация'})
        self.assertEqual(r.status_code,302)
        p=DocumentProfile.objects.get(name='Документ организации')
        self.assertEqual(p.revision,1)
        self.assertContains(self.client.get(r.url),'Версия 1')

    def test_shared_glossary_extends_sources_preserving_custom_predicate(self):
        from .profiles import activate_extracted_profiles
        from types import SimpleNamespace
        sid=self.prepared();source=SourceUpload.objects.get(pk=sid)
        def add(identity):
            src=SimpleNamespace(pk=identity,normative_set=source.normative_set)
            profile=dict(id=str(uuid.uuid4()),key='glossary',name='Glossary',version=1,
                         expression={'fact':{'name':'selected_sources','in':[str(identity)]}},
                         basis=[{'locator':'p1'}],inherits=[])
            activate_extracted_profiles(SimpleNamespace(pk=uuid.uuid4(),actor=self.user),src,[profile])
        add(source.pk);other=uuid.uuid4();add(other)
        row=DocumentProfile.objects.get(name='Glossary')
        self.assertEqual(set(row.definition['expression']['fact']['in']),{sid,str(other)})
        custom={'fact':{'name':'organization','in':['Custom']}}
        row.definition['expression']=custom;row.save()
        add(uuid.uuid4());row.refresh_from_db()
        self.assertEqual(row.definition['expression'],custom)
        self.assertEqual(len(row.definition['bindings']),3)
