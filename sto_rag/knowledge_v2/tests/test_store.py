import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from knowledge_v2.store import KnowledgeStore, Conflict, NotReady, checksum


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=KnowledgeStore(self.tmp.name)
        self.store.register_set('set','scope',{'name':'Example'})

    def release(self,rid='release',generation='generation',space='space1'):
        self.store.put_record('set','source_revision','source',1,{'sha256':'a'*64,'original_key':'sha256/aa','parser_version':'p1'})
        self.store.put_record('set','fragment','fragment',1,{'source_revision':['source',1],'locator':'p1','exact_text':'Must report errors.','search_text':'report errors','context_hash':'c'},refs=[('source',1)])
        m=self.store.create_release('set',rid,generation,space,[('source',1),('fragment',1)],{'parser':'p1'})
        return m

    def ready(self,rid='release',generation='generation',space='space1'):
        m=self.release(rid,generation,space)
        att=dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space=space,record_count=2,watermark=1)
        return self.store.attest_ready(rid,att)

    def publish(self,rid='release',generation='generation',space='space1'):
        payload=self.ready(rid,generation,space)
        self.store.apply_command('pub-'+rid,'release.publish',{'set_id':'set','release_id':rid,'manifest_hash':payload['manifest_hash']})

    def test_empty_store_requires_explicit_path_and_does_not_import(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaises(ValueError):KnowledgeStore()
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'catalog-current.json').write_text('{"version":"legacy"}')
            self.assertEqual(KnowledgeStore(t).counts()['records'],0)
            self.assertEqual(KnowledgeStore(t).counts()['releases'],0)

    def test_restart_preserves_records_and_events(self):
        self.ready();counts=self.store.counts()
        again=KnowledgeStore(self.tmp.name)
        self.assertEqual(counts,again.counts());self.assertEqual(len(again.pending_events()),1)

    def test_record_version_idempotence_and_immutability(self):
        self.release()
        with self.assertRaises(Conflict):self.store.put_record('set','source_revision','source',1,{'sha256':'b'*64,'original_key':'other','parser_version':'p1'})
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection() as db:db.execute("UPDATE records SET payload='{}'")

    def test_reference_closure_and_scope(self):
        self.release()
        with self.assertRaises(ValueError):self.store.create_release('set','r2','g2','s',[('fragment',1)],{'parser':'p1'})
        self.store.register_set('other','different',{})
        with self.assertRaises(ValueError):self.store.create_release('other','r2','g2','s',[('source',1)],{'parser':'p1'})

    def test_no_publishing_before_both_indexes_ready(self):
        m=self.release()
        with self.assertRaises(NotReady):self.store.apply_command('pub','release.publish',{'set_id':'set','release_id':'release','manifest_hash':checksum(m)})
        with self.assertRaises(NotReady):self.store.attest_ready('release',dict(canonical=True,fts=True,vector=False,provenance=True,manifest_hash=checksum(m),embedding_space='space1',record_count=2,watermark=1))

    def test_embedding_space_and_manifest_are_checked(self):
        m=self.release()
        with self.assertRaises(NotReady):self.store.attest_ready('release',dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(m),embedding_space='same-dimension-different-model',record_count=2,watermark=1))

    def test_snapshot_stays_pinned_after_new_release(self):
        self.publish();snap=self.store.pin_snapshot('snapshot','job',['release'],{'model':'a'},lambda _:True)
        self.publish('release2','generation2','space2')
        self.assertEqual(snap,self.store.read_snapshot('snapshot',lambda _:True))
        with self.assertRaises(Conflict):self.store.pin_snapshot('snapshot','job',['release2'],{'model':'a'},lambda _:True)
        with self.assertRaises(PermissionError):self.store.read_snapshot('snapshot',lambda _:False)

    def test_empty_snapshot_is_not_normative_success(self):
        with self.assertRaises(NotReady):self.store.pin_snapshot('s','j',[],{'model':'a'},lambda _:True)

    def test_inbox_deduplicates_and_checks_collision(self):
        p={'set_id':'new','scope_id':'scope','metadata_revision':1,'metadata':{'name':'New'}}
        first=self.store.apply_command('cmd','set.register',p)
        self.assertEqual(first,self.store.apply_command('cmd','set.register',p))
        with self.assertRaises(Conflict):self.store.apply_command('cmd','set.register',{**p,'scope_id':'other'})
        self.assertEqual(len(self.store.pending_events()),1)

    def test_outbox_failure_rolls_back_canonical_transaction(self):
        p={'set_id':'new','scope_id':'scope','metadata_revision':1,'metadata':{}}
        with patch.object(self.store,'_event',side_effect=RuntimeError('power loss')):
            with self.assertRaises(RuntimeError):self.store.apply_command('cmd','set.register',p)
        self.assertEqual(self.store.counts()['sets'],1)
        self.store.apply_command('cmd','set.register',p)
        event=self.store.pending_events()[0]
        with self.assertRaises(Conflict):self.store.acknowledge(event['id'],'bad')
        self.store.acknowledge(event['id'],event['payload_hash']);self.assertFalse(self.store.pending_events())

    def test_tasks_recover_with_cursor_and_reject_stale_lease(self):
        tid=self.store.enqueue('extract','k',{'source':'s'},max_attempts=2)
        first=self.store.claim();self.store.checkpoint(tid,first['lease'],{'page':3})
        second=self.store.claim(now=time.time()+121)
        self.assertEqual(second['cursor'],{'page':3})
        with self.assertRaises(Conflict):self.store.checkpoint(tid,first['lease'],{},True)
        self.assertIsNone(self.store.claim(now=time.time()+242))
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT state FROM tasks').fetchone()[0],'failed')

    def test_non_applicability_requires_evidence(self):
        with self.assertRaises(ValueError):self.store.put_record('set','applicability','app',1,dict(requirement_ref=['r',1],result='not_applicable',criteria={},evidence=[]))

    def test_payload_references_cannot_bypass_scope_validation(self):
        self.release();self.store.register_set('other','other-scope',{})
        with self.assertRaises(ValueError):
            self.store.put_record('other','fragment','hidden',1,dict(source_revision=['source',1],locator='p1',exact_text='private',search_text='private',context_hash='x'))
        with self.assertRaises(Conflict):
            self.store.put_record('other','source_revision','source',2,dict(sha256='a'*64,original_key='sha256/a',parser_version='p1'))

    def test_revoke_blocks_new_snapshot_but_preserves_history(self):
        self.publish();self.store.pin_snapshot('s','job',['release'],{'model':'a'},lambda _:True)
        self.store.revoke_release('release','new edition required')
        with self.assertRaises(NotReady):self.store.pin_snapshot('s2','job2',['release'],{'model':'a'},lambda _:True)
        self.assertEqual(self.store.read_snapshot('s',lambda _:True)['releases'][0]['release_id'],'release')

    def test_consistent_backup_restores_to_separate_directory(self):
        self.publish()
        with tempfile.TemporaryDirectory() as target:
            self.store.backup(Path(target)/'knowledge-v2.sqlite3')
            restored=KnowledgeStore(target)
            self.assertEqual(restored.counts(),self.store.counts())
            with self.assertRaises(Conflict):self.store.backup(Path(target)/'knowledge-v2.sqlite3')

    def test_concurrent_task_claim_only_one_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.store.enqueue('extract','one',{})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.store.claim(),range(2)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_concurrent_first_initialization_is_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as target:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:KnowledgeStore(target).counts(),range(2)))
            self.assertEqual(results[0],results[1])


if __name__=='__main__':unittest.main()
