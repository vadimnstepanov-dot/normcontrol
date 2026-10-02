import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from native_core.output_guard import compact_schema, split_payload, recover_overflow, MAX_SPLIT_DEPTH
from nc5.model import SCHEMA
from nc5.store import Store
from nc5.common import digest


class OutputGuardTests(unittest.TestCase):
    def test_explanations_bounded_but_sources_and_coverage_not_cut(self):
        schema=compact_schema(copy.deepcopy(SCHEMA))
        finding=schema['properties']['findings']['items']['properties']
        self.assertEqual(finding['explanation']['maxLength'],900)
        self.assertNotIn('maxLength',finding['evidence']['items']['properties']['quote'])
        self.assertNotIn('maxItems',schema['properties']['findings'])
        self.assertNotIn('maxItems',schema['properties']['coverage'])
        self.assertNotIn('maxLength',SCHEMA['properties']['findings']['items']['properties']['explanation'])

    def test_extra_split_preserves_complete_rows_and_every_block(self):
        blocks=[{'document':'d','locator':f'p{i}','text':str(i), 'table':{'table':1,'row':i//3}}
                for i in range(9)]
        task={'id':'t','job':'j','stage':'logic','payload':{'stage':'logic','blocks':blocks,'retry_split':2}}
        parts=split_payload(SimpleNamespace(config={'output':4096}),task)
        self.assertEqual([b for p in parts for b in p['blocks']],blocks)
        self.assertNotEqual(parts[0]['blocks'][-1]['table']['row'],parts[1]['blocks'][0]['table']['row'])
        self.assertTrue(all(p['output_parent_task']=='t' and p['output_split_depth']==3 for p in parts))
        self.assertTrue(all(not p['source_inventory']['document_complete'] for p in parts))

    def test_finite_depth_and_no_unsafe_visual_split(self):
        for payload in ({'output_split_depth':MAX_SPLIT_DEPTH},{'images':[{}]}):
            with self.assertRaises(ValueError):
                split_payload(SimpleNamespace(config={'output':4096}),{'id':'t','job':'j','stage':'logic','payload':payload})

    def test_failed_parent_recovered_without_touching_done_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db')
            job=store.create({'options':{}});store.update(job,'running')
            good=store.add(job,'language',{'stage':'language','blocks':[]})
            store.finish(store.claim(job,'language'),{'raw':'accepted'})
            payload={'stage':'language','blocks':[{'document':'d','locator':f'p{i}','text':'text'} for i in range(4)],'retry_split':2}
            parent=store.add(job,'language',payload);task=store.claim(job,'language')
            store.finish(task,{'finish_reason':'length','metrics':{'usage':{'completion_tokens':4096}}},'Незавершённый ответ: length')
            instance=SimpleNamespace(store=store,config={'output':4096},enqueue_bounded=lambda j,s,p,d:store.add(j,s,p))
            self.assertTrue(recover_overflow(instance,task))
            tasks={t['id']:t for t in store.tasks(job)}
            self.assertEqual(tasks[good]['state'],'done');self.assertEqual(tasks[good]['result'],{'raw':'accepted'})
            self.assertEqual(tasks[parent]['state'],'split');self.assertIsNone(tasks[parent]['error'])
            self.assertEqual(tasks[parent]['result']['metrics']['usage']['completion_tokens'],4096)
            self.assertEqual(len(tasks[parent]['result']['children']),2)
            self.assertTrue(all(tasks[i]['state']=='pending' for i in tasks[parent]['result']['children']))
            self.assertFalse(recover_overflow(instance,task))

    def test_non_overflow_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db');job=store.create({'options':{}});store.update(job,'running')
            store.add(job,'logic',{'stage':'logic','blocks':[]});task=store.claim(job,'logic');store.finish(task,error='timeout')
            self.assertFalse(recover_overflow(SimpleNamespace(store=store),task))
            self.assertEqual(store.tasks(job)[0]['state'],'failed')

    def test_pause_during_split_leaves_parent_pending_and_resumable(self):
        from pipeline import QueuePaused
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db');job=store.create({'options':{}});store.update(job,'running')
            payload={'stage':'language','blocks':[{'text':'a'},{'text':'b'}],'retry_split':2}
            store.add(job,'language',payload);task=store.claim(job,'language');store.finish(task,{'finish_reason':'length'},'Незавершённый ответ: length')
            def enqueue(*args):raise QueuePaused()
            instance=SimpleNamespace(store=store,config={'output':4096},enqueue_bounded=enqueue)
            self.assertFalse(recover_overflow(instance,task))
            self.assertEqual(store.tasks(job)[0]['state'],'pending')
            self.assertEqual(store.tasks(job)[0]['attempts'],1)

    def test_request_change_is_part_of_cache_key(self):
        from native_core.output_guard import install
        from nc5.model import Client
        from nc5.common import config
        from nc5.engine import response_cache_key
        payload={'stage':'language','blocks':[],'requirements':[]}
        client=Client(config());client.signature='same-model'
        before=response_cache_key(client,payload)
        # Restore patches after test so other nc5 tests keep their native contracts.
        from nc5 import model,engine
        originals=(model.output_budget,engine.output_budget,Client.request,Client.http,engine.Engine.execute)
        try:
            install()
            self.assertNotEqual(before,response_cache_key(client,payload))
            self.assertEqual(client.request(payload)['max_tokens'],min(2048,client.config['output']))
            self.assertEqual(model.output_budget(client.config,{'stage':'logic'}),4096)
            self.assertLessEqual(model.output_budget(client.config,{**payload,'_output_budget':8192}),client.config['output'])
            install()  # idempotent
        finally:
            model.output_budget,engine.output_budget,Client.request,Client.http,engine.Engine.execute=originals
            del model._native_output_guard

if __name__=='__main__':unittest.main()
