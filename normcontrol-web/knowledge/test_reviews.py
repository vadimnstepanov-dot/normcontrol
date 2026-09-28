import json
import tempfile
import uuid
from pathlib import Path
from django.contrib.auth import get_user_model
from django.test import TestCase,Client,override_settings
from . import services
from .models import Membership,ExperienceReview
from knowledge_v2.store import KnowledgeStore,checksum
from knowledge_v2.bridge import Bridge
from knowledge_v2.tests import test_review
from knowledge_v2.review import ReviewRunner
from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors

TOKEN='stage7-test-token-not-a-real-secret-123456789'


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='stage7-worker')
class ReviewAPITests(TestCase):
    def setUp(self):
        User=get_user_model()
        self.admin=User.objects.create_user('curator',is_staff=True)
        self.author=User.objects.create_user('review-author')
        self.reader=User.objects.create_user('read-only')
        self.scope=services.create_scope(self.admin,'Team','organization')
        Membership.objects.create(scope=self.scope,user=self.author,role='reviewer')
        Membership.objects.create(scope=self.scope,user=self.reader,role='reader')
        self.norms=services.create_set(self.admin,'Norms',self.scope.pk,'norms')
        self.lessons=services.create_set(self.admin,'Experience',self.scope.pk,'lessons',purpose='experience')
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=KnowledgeStore(tmp.name)
        self.worker=Client();self.bridge=Bridge(self.store,'http://localhost/normcontol/api/v2',TOKEN,self.transport,experience_index=(FakeEncoder(),FakeVectors()))
        self.bridge.once();self.bridge.once()
        fixture=test_review.ReviewTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with fixture.store.connection() as db:
            for r in db.execute('SELECT * FROM records ORDER BY rowid'):
                self.store.put_record(str(self.norms.pk),r['kind'],r['id'],r['version'],json.loads(r['payload']))
        with self.store.connection() as db:refs=[(r['id'],r['version']) for r in db.execute('SELECT id,version FROM records')]
        self.release=str(uuid.uuid4());sid=str(self.norms.pk)
        m=self.store.create_release(sid,self.release,str(uuid.uuid4()),'test-space',refs,{'test':'1'})
        self.store.attest_ready(self.release,dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space='test-space',record_count=len(refs),watermark=1))
        self.store.apply_command('publish','release.publish',dict(set_id=sid,release_id=self.release,manifest_hash=checksum(m)))
        runner=ReviewRunner(self.store,fixture.model,self.bridge.authorization_for(self.author.pk),owner=self.author.pk)
        task=runner.create([fixture.path],{fixture.did:{self.release:['profile']}},fixture.facts,lambda *args:True)
        runner.run_once();report=runner.report(task)
        with self.store.connection() as db:p=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task,)).fetchone()[0])
        b=p['documents'][0]['blocks'][1]
        draft=dict(summary='Проверять срок с учётом среды.',conditions={'fact':{'name':'environment','in':['production']}},
            counter_conditions={'fact':{'name':'environment','in':['test']}},counterexample='Тестовая среда.',
            required_evidence='Среда, срок и норматив.',stages=['check','verify'],scope_id=str(self.scope.pk),sharing_confirmed=True,
            normative_refs=[dict(set_id=sid,release_id=self.release,requirement_ref=['r',1])])
        self.data=dict(task_id=task,obligation_id=report['decisions'][0]['obligation']['id'],proposal_kind='private_example',
            comment='Необходимо учитывать среду при сопоставлении сроков.',evidence=[dict(block_id=b['id'],quote=b['text'])],draft=draft)
        self.client.force_login(self.author)

    def transport(self,path,payload):
        r=self.worker.post('/normcontol/api/v2'+path,json.dumps(payload),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+TOKEN)
        if r.status_code!=200:raise RuntimeError((r.status_code,r.json()))
        return r.json()

    def post(self,path,data,key):
        return self.client.post('/normcontol/api/v2'+path,json.dumps(data),content_type='application/json',HTTP_IDEMPOTENCY_KEY=key)

    def submit(self):
        r=self.post(f'/normative-sets/{self.lessons.pk}/reviews/',self.data,'submit')
        self.assertEqual(r.status_code,202,r.content);self.bridge.once()
        return r.json()['id']

    def test_full_api_bridge_curation_revoke_and_restore(self):
        rid=self.submit();obj=ExperienceReview.objects.get(pk=rid);self.assertEqual(obj.state,'pending')
        body=dict(expected_revision=obj.revision,draft=self.data['draft'])
        denied=self.post(f'/reviews/{rid}/approve/',body,'denied');self.assertEqual(denied.status_code,403)
        self.client.force_login(self.admin)
        reply=self.post(f'/reviews/{rid}/approve/',body,'approve');self.assertEqual(reply.status_code,202,reply.content)
        self.bridge.once();obj.refresh_from_db();self.assertEqual(obj.state,'approved')
        published=self.post(f'/normative-sets/{self.lessons.pk}/experience/publish/',{'expected_revision':1},'publish-experience')
        self.assertEqual(published.status_code,202,published.content);self.bridge.once()
        self.lessons.refresh_from_db();self.assertIsNotNone(self.lessons.active_release_id)
        self.assertEqual(self.lessons.active_release.state,'active')
        retry=self.post(f'/reviews/{rid}/approve/',body,'approve');self.assertEqual(retry.status_code,202)
        revoke=self.post(f'/reviews/{rid}/revoke/',dict(expected_revision=obj.revision,reason='Уточнить область'),'revoke')
        self.assertEqual(revoke.status_code,202,revoke.content);self.bridge.once();obj.refresh_from_db();self.assertEqual(obj.state,'revoked')
        restored=self.post(f'/reviews/{rid}/approve/',dict(expected_revision=obj.revision,draft=self.data['draft']),'restore')
        self.assertEqual(restored.status_code,202,restored.content);self.bridge.once();obj.refresh_from_db()
        self.assertEqual(obj.result['version'],2)

    def test_reader_cannot_submit_or_read_original_review(self):
        rid=self.submit();self.client.force_login(self.reader)
        self.assertEqual(self.client.get(f'/normcontol/api/v2/reviews/{rid}/').status_code,403)
        self.assertEqual(self.post(f'/normative-sets/{self.lessons.pk}/reviews/',self.data,'reader').status_code,403)

    def test_finding_status_is_not_a_review_and_sharing_is_explicit(self):
        self.assertEqual(self.post(f'/normative-sets/{self.lessons.pk}/reviews/',{'status':'fixed'},'status').status_code,400)
        self.data['draft']['sharing_confirmed']=False
        rid=self.submit();self.client.force_login(self.admin)
        self.assertEqual(self.post(f'/reviews/{rid}/approve/',dict(expected_revision=1,draft=self.data['draft']),'approve').status_code,403)
