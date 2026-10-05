import tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from knowledge_v2.review_client import LlamaClient
from knowledge_v2.store import KnowledgeStore
from knowledge_v2.check_runtime import execute,PreparationPaused

class PlanningCacheTests(unittest.TestCase):
 def test_new_plan_compacts_gaps_and_resume_keeps_original_wire(self):
  from knowledge_v2.check_runtime import review_wire_version
  from knowledge_v2.review_wire import VERSION,GROUPED_VERSION,COMPACT_NORMS_VERSION
  with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{},clear=True):
   store=KnowledgeStore(folder)
   self.assertEqual(review_wire_version(store,'new'),COMPACT_NORMS_VERSION)
   store.enqueue('review.run','review.run:old',{'snapshot':{'versions':{'transport':VERSION}}})
   self.assertEqual(review_wire_version(store,'old'),VERSION)
   with patch.dict('os.environ',{'NORMCONTROL_REVIEW_WIRE':GROUPED_VERSION}):
    self.assertEqual(review_wire_version(store,'old'),VERSION)

 def client(self,store,signature='model-a'):
  c=LlamaClient.__new__(LlamaClient);c.store=store;c.signature=signature;c._counts={};c._schema_counts={}
  c.request=lambda p:{'messages':[{'content':p['text']}],'response_format':{'schema':'stable'}}
  c.http=Mock(side_effect=lambda path,value:{'prompt':value['messages'][0]['content']} if path=='/apply-template' else {'tokens':[1,2,3]})
  return c
 def test_cache_survives_client_restart_and_invalidates_model_and_prompt(self):
  with tempfile.TemporaryDirectory() as folder:
   store=KnowledgeStore(folder);a=self.client(store);expected=a.count({'text':'private-document-fixture'})
   b=self.client(store);self.assertEqual(b.count({'text':'private-document-fixture'}),expected);b.http.assert_not_called()
   changed=self.client(store,'model-b');changed.count({'text':'private-document-fixture'});self.assertTrue(changed.http.called)
   b.count({'text':'changed-fixture'});self.assertTrue(b.http.called)
   with store.connection() as db:
    rows=db.execute('SELECT * FROM token_measurements').fetchall()
   self.assertNotIn('private-document-fixture',str([dict(r) for r in rows]))
 def test_pause_before_plan_returns_legitimate_empty_ledger(self):
  with tempfile.TemporaryDirectory() as folder:
   store=KnowledgeStore(folder);bridge=SimpleNamespace(store=store)
   claim={'command_id':'command','payload':{'job_id':'job','set_id':'set','logging':{'enabled':False}}}
   with patch('knowledge_v2.check_runtime._execute',side_effect=PreparationPaused):result=execute(bridge,claim,None)
   self.assertEqual(result['state'],'paused');self.assertIsNone(result['task_id']);self.assertEqual(result['finding_count'],0)
 def test_preparation_pause_cannot_overwrite_saved_plan(self):
  with tempfile.TemporaryDirectory() as folder:
   store=KnowledgeStore(folder);store.enqueue('review.run','review.run:job',{})
   claim={'command_id':'command','payload':{'job_id':'job','set_id':'set','logging':{'enabled':False}}}
   with patch('knowledge_v2.check_runtime._execute',side_effect=PreparationPaused):
    with self.assertRaises(PreparationPaused):execute(SimpleNamespace(store=store),claim,None)
