import json
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from portal.models import AccessProfile, Batch, WorkerRun
from . import services as s
from .models import Scope, Membership, NormativeSet, Release, Snapshot, Command, Receipt

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'sto_rag'))
from knowledge_v2.store import KnowledgeStore, checksum
from knowledge_v2.bridge import Bridge

TOKEN='test-only-v2-token-not-a-real-secret-12345'


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class KnowledgeTests(TestCase):
    def setUp(self):
        User=get_user_model()
        self.owner=User.objects.create_user('owner-v2')
        self.other=User.objects.create_user('other-v2')
        self.admin=User.objects.create_user('admin-v2',is_staff=True)
        self.scope=s.create_scope(self.owner,'Personal','personal')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(self.tmp.name)
        self.wclient=Client()
        self.bridge=Bridge(self.store,'http://localhost/normcontol/api/v2',TOKEN,self.transport)
        self.dataset=s.create_set(self.owner,'New norms',self.scope.pk,'set-1')
        self.client.force_login(self.owner)

    def transport(self,path,payload):
        r=self.wclient.post('/normcontol/api/v2'+path,data=json.dumps(payload),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+TOKEN)
        if r.status_code!=200:raise RuntimeError((r.status_code,r.json()))
        return r.json()

    def prepare(self):
        self.bridge.once()
        rid=str(uuid.uuid4());sid=str(self.dataset.pk)
        self.store.put_record(sid,'source_revision','source',1,{'sha256':'a'*64,'original_key':'sha256/aa','parser_version':'p1'})
        versions={'parser':'p1'}
        manifest=self.store.create_release(sid,rid,str(uuid.uuid4()),'test-space',[('source',1)],versions)
        c=s.enqueue_preparation(self.owner,self.dataset.pk,rid,['source'],versions,str(uuid.uuid4()))
        delivery=s.claim('test-worker',['release.prepare'])
        att=dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(manifest),embedding_space='test-space',record_count=1,watermark=1)
        result={'kind':'release.ready',**self.store.attest_ready(rid,att)}
        s.accept_event('test-worker',uuid.uuid4(),c.pk,delivery['lease'],result,s.digest(result))
        return rid

    def publish(self):
        rid=self.prepare();self.dataset.refresh_from_db()
        c=s.publish(self.owner,self.dataset.pk,rid,self.dataset.metadata_revision,str(uuid.uuid4()))
        self.bridge.once();self.dataset.refresh_from_db();c.refresh_from_db()
        self.assertEqual(c.state,'done');self.assertEqual(str(self.dataset.active_release_id),rid)
        return rid

    def test_creation_empty_and_no_legacy_queue_mutation(self):
        b=Batch.objects.create(owner=self.owner,name='Legacy',status='running')
        run=WorkerRun.objects.create(batch=b,worker='v1',state='running',local_id='old',sequence=12)
        self.assertEqual(self.store.counts()['records'],0)
        self.bridge.once()
        self.assertEqual(self.store.counts()['sets'],1)
        self.assertEqual(self.store.counts()['releases'],0)
        b.refresh_from_db();run.refresh_from_db()
        self.assertEqual((b.status,run.state,run.sequence),('running','running',12))
        self.assertFalse(self.bridge.once())

    def test_idempotent_create_and_key_collision(self):
        again=s.create_set(self.owner,'New norms',self.scope.pk,'set-1')
        self.assertEqual(again.pk,self.dataset.pk)
        self.assertEqual(Command.objects.count(),1)
        with self.assertRaises(s.Conflict):s.create_set(self.owner,'different',self.scope.pk,'set-1')

    def test_private_scope_ignores_view_others_flag(self):
        AccessProfile.objects.create(user=self.other,can_view_others=True)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/normcontol/api/v2/normative-sets/').json()['sets'],[])
        self.assertEqual(self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/').status_code,403)
        with self.assertRaises(PermissionDenied):s.create_set(self.other,'Unauthorized',self.scope.pk,'key')

    def test_worker_search_authorization_is_fresh_and_scoped(self):
        authorize=self.bridge.authorization_for(self.owner.pk)
        self.assertTrue(authorize(str(self.dataset.pk)))
        self.assertFalse(self.bridge.authorization_for(self.other.pk)(str(self.dataset.pk)))
        self.owner.is_active=False;self.owner.save(update_fields=['is_active'])
        self.assertFalse(authorize(str(self.dataset.pk)))

    def test_api_csrf_and_actor_spoofing(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.owner)
        url='/normcontol/api/v2/normative-sets/'
        self.assertEqual(c.post(url,data='{}',content_type='application/json').status_code,403)
        r=self.client.post(url,data=json.dumps({'name':'Bad','scope_id':str(self.scope.pk),'owner_id':self.admin.pk}),content_type='application/json',HTTP_IDEMPOTENCY_KEY='k')
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.wclient.post('/normcontol/api/v2/worker/claim/',data='{}',content_type='application/json').status_code,403)

    def test_empty_set_cannot_be_snapshot(self):
        with self.assertRaises(s.NotReady):s.create_snapshot(self.owner,uuid.uuid4(),[self.dataset.pk],{'model':'m'})
        self.assertEqual(Snapshot.objects.count(),0)

    def test_publish_roundtrip_and_snapshot_pin(self):
        first=self.publish()
        snap=s.create_snapshot(self.owner,uuid.uuid4(),[self.dataset.pk],{'model':'m'})
        second=self.publish();self.assertNotEqual(first,second)
        loaded=s.read_snapshot(self.owner,snap.pk)
        self.assertEqual(loaded.data['releases'][0]['release_id'],first)
        with self.assertRaises(s.Conflict):s.create_snapshot(self.owner,snap.job_id,[self.dataset.pk],{'model':'m'})

    def test_index_readiness_not_inferred_from_fake_event(self):
        self.bridge.once();rid=str(uuid.uuid4())
        receipts_before=Receipt.objects.count()
        c=s.enqueue_preparation(self.owner,self.dataset.pk,rid,['source'],{'parser':'p1'},'prep')
        delivery=s.claim('test-worker',['release.prepare'])
        bad={'kind':'release.ready','set_id':str(self.dataset.pk),'release_id':rid,'manifest':{},'manifest_hash':s.digest({}),'attestation':{}}
        with self.assertRaises(s.NotReady):s.accept_event('test-worker',uuid.uuid4(),c.pk,delivery['lease'],bad,s.digest(bad))
        self.assertFalse(Release.objects.exists());self.assertEqual(Receipt.objects.count(),receipts_before)

    def test_ack_lost_after_portal_commit_is_redelivered_after_restart(self):
        def unreliable(path,payload):
            response=self.transport(path,payload)
            if path=='/worker/events/':raise ConnectionError('response lost after commit')
            return response
        bridge=Bridge(self.store,'http://localhost/normcontol/api/v2',TOKEN,unreliable)
        with self.assertRaises(ConnectionError):bridge.once()
        self.assertEqual(Command.objects.get().state,'done')
        restarted=Bridge(KnowledgeStore(self.tmp.name),'http://localhost/normcontol/api/v2',TOKEN,self.transport)
        self.assertTrue(restarted.once())
        self.assertEqual(Receipt.objects.count(),1)
        self.assertFalse(self.store.pending_events())

    def test_lost_reply_retries_without_duplicate(self):
        claimed=s.claim('test-worker',['set.register'])
        result=self.store.apply_command(claimed['command_id'],claimed['kind'],claimed['payload'])
        # Local transaction committed, process died before HTTP delivery.
        Command.objects.filter(pk=claimed['command_id']).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.bridge.once()
        self.assertEqual(self.store.counts()['sets'],1)
        self.assertEqual(Receipt.objects.count(),1)
        self.assertEqual(Command.objects.get().attempts,2)

    def test_duplicate_event_and_payload_collision(self):
        delivery=s.claim('test-worker',['set.register'])
        result=self.store.apply_command(delivery['command_id'],delivery['kind'],delivery['payload'])
        eid=uuid.uuid4()
        first=s.accept_event('test-worker',eid,delivery['command_id'],delivery['lease'],result,s.digest(result))
        self.assertEqual(first,s.accept_event('test-worker',eid,delivery['command_id'],delivery['lease'],result,s.digest(result)))
        altered={**result,'extra':True}
        with self.assertRaises(s.Conflict):s.accept_event('test-worker',eid,delivery['command_id'],delivery['lease'],altered,s.digest(altered))

    def test_stale_lease_rejected(self):
        old=s.claim('test-worker',['set.register'])
        Command.objects.filter(pk=old['command_id']).update(lease_until=timezone.now()-timedelta(seconds=1))
        fresh=s.claim('test-worker',['set.register'])
        result={'kind':'set.registered','set_id':str(self.dataset.pk)}
        with self.assertRaises(PermissionDenied):s.accept_event('test-worker',uuid.uuid4(),old['command_id'],old['lease'],result,s.digest(result))
        self.assertNotEqual(old['lease'],fresh['lease'])

    def test_outbox_atomic_with_portal_creation(self):
        before=NormativeSet.objects.count()
        with patch('knowledge.services.command',side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):s.create_set(self.owner,'Rollback',self.scope.pk,'rollback')
        self.assertEqual(NormativeSet.objects.count(),before)

    def test_scope_inheritance_and_revocation_on_snapshot(self):
        org=s.create_scope(self.admin,'Org','organization')
        team=s.create_scope(self.admin,'Team','team',org.pk)
        project=s.create_scope(self.admin,'Project','project',team.pk)
        s.set_membership(self.admin,org.pk,self.owner,'curator')
        self.dataset=s.create_set(self.owner,'Project norms',project.pk,'project-set')
        # Register both pending datasets before preparing the selected one.
        while self.bridge.once():pass
        self.publish();snap=s.create_snapshot(self.owner,uuid.uuid4(),[self.dataset.pk],{'model':'m'})
        s.set_membership(self.admin,org.pk,self.owner,None)
        with self.assertRaises(PermissionDenied):s.read_snapshot(self.owner,snap.pk)
        self.assertTrue(Snapshot.objects.filter(pk=snap.pk).exists())

    def test_contributor_does_not_have_publish_or_manage(self):
        team=s.create_scope(self.admin,'Team','team')
        s.set_membership(self.admin,team.pk,self.other,'contributor')
        ds=s.create_set(self.other,'Allowed upload',team.pk,'contribution')
        with self.assertRaises(PermissionDenied):s.publish(self.other,ds.pk,uuid.uuid4(),1,'p')
        with self.assertRaises(PermissionDenied):s.set_membership(self.other,team.pk,self.other,'manager')

    def test_revoked_creator_cannot_finish_claimed_command(self):
        self.bridge.once()
        team=s.create_scope(self.admin,'Team','team')
        s.set_membership(self.admin,team.pk,self.other,'contributor')
        ds=s.create_set(self.other,'New',team.pk,'new')
        delivery=s.claim('test-worker',['set.register'])
        s.set_membership(self.admin,team.pk,self.other,None)
        result={'kind':'set.registered','set_id':str(ds.pk)}
        with self.assertRaises(PermissionDenied):s.accept_event('test-worker',uuid.uuid4(),delivery['command_id'],delivery['lease'],result,s.digest(result))

    def test_attempts_bounded_and_no_legacy_claim(self):
        c=Command.objects.get()
        for _ in range(3):
            self.assertIsNotNone(s.claim('test-worker',['set.register']))
            Command.objects.filter(pk=c.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(s.claim('test-worker',['set.register']))
        c.refresh_from_db();self.assertEqual(c.state,'failed')

    def test_api_does_not_accept_user_supplied_runtime_versions(self):
        r=self.client.post('/normcontol/api/v2/snapshots/',data=json.dumps({'job_id':str(uuid.uuid4()),'set_ids':[str(self.dataset.pk)],'versions':{'model':'spoof'}}),content_type='application/json')
        self.assertEqual(r.status_code,400)

    def test_publish_requires_idempotency_key(self):
        with self.assertRaises(ValueError):s.publish(self.owner,self.dataset.pk,uuid.uuid4(),1,None)

    def test_rollback_ready_event_does_not_leave_receipt_or_release(self):
        self.bridge.once();rid=str(uuid.uuid4());sid=str(self.dataset.pk)
        self.store.put_record(sid,'source_revision','source',1,{'sha256':'a'*64,'original_key':'sha256/aa','parser_version':'p1'})
        m=self.store.create_release(sid,rid,str(uuid.uuid4()),'space',[('source',1)],{'parser':'p1'})
        c=s.enqueue_preparation(self.owner,self.dataset.pk,rid,['source'],{'parser':'p1'},'prep-fault')
        delivery=s.claim('test-worker',['release.prepare'])
        att=dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space='space',record_count=1,watermark=1)
        payload=self.store.attest_ready(rid,att);count=Receipt.objects.count()
        with patch.object(Receipt.objects,'create',side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):s.accept_event('test-worker',uuid.uuid4(),c.pk,delivery['lease'],payload,s.digest(payload))
        self.assertFalse(Release.objects.exists());self.assertEqual(Receipt.objects.count(),count)
        c.refresh_from_db();self.assertEqual(c.state,'delivering')
