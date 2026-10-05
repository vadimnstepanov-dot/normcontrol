import threading, tempfile, unittest, uuid, json
from types import SimpleNamespace
from unittest.mock import patch
from knowledge_v2.lease_heartbeat import renew_analysis_lease, ReviewLeaseLost
from knowledge_v2.check_runtime import execute
from knowledge_v2.bridge import Bridge
from knowledge_v2.store import KnowledgeStore
from .test_lease_heartbeat import ClockAndStop

class ReviewLeaseTests(unittest.TestCase):
 def test_expired_lease_does_not_download_or_create_result(self):
  with tempfile.TemporaryDirectory() as d:
   bridge=SimpleNamespace(store=KnowledgeStore(d))
   with patch('knowledge_v2.check_runtime._execute') as operation:
    operation.side_effect=ReviewLeaseLost('lost')
    with self.assertRaises(ReviewLeaseLost):execute(bridge,{'payload':{'job_id':str(uuid.uuid4())}},None,lease_cancel=lambda:True)
   with bridge.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0],0)
 def test_runtime_checks_loss_before_authorization_download_or_plan(self):
  from knowledge_v2.check_runtime import _execute
  bridge=SimpleNamespace(transport=lambda *a:self.fail('stale progress was sent'))
  with self.assertRaises(ReviewLeaseLost):_execute(bridge,{},None,lease_cancel=lambda:True)
 def test_slow_response_does_not_reset_budget_from_response_receipt(self):
  stopped=ClockAndStop(3);lost=threading.Event();events=[]
  def slow(*args):stopped.now+=580;return {}
  renew_analysis_lease(slow,{'command_id':'id','lease':'secret'},stopped,lost,clock=lambda:stopped.now,observe=events.append)
  self.assertTrue(lost.is_set());self.assertEqual(events[-1]['reason'],'renewal_response_too_late')
  self.assertNotIn('secret',json.dumps(events))
 def test_diagnostic_failure_does_not_interrupt_renewal(self):
  stopped=ClockAndStop(1);lost=threading.Event()
  def broken(event):raise OSError('disk')
  renew_analysis_lease(lambda *a:{},{'command_id':'id','lease':'secret'},stopped,lost,clock=lambda:stopped.now,observe=broken)
  self.assertFalse(lost.is_set())
 def test_bridge_passes_revocation_guard_and_keeps_failure_unpublished(self):
  with tempfile.TemporaryDirectory() as d:
   claim={'kind':'review.execute','command_id':str(uuid.uuid4()),'lease':'private-lease','payload':{}}
   bridge=Bridge(KnowledgeStore(d),'https://example.test','x'*32,transport=lambda *a:{'command':claim})
   ready=threading.Event()
   def renewal(transport,claim,stopped,lost,**kwargs):
    kwargs['observe']({'state':'lost','status':409});lost.set();ready.set()
   def operation(*args,**kwargs):
    self.assertTrue(ready.wait(1));self.assertTrue(kwargs['lease_cancel']());raise ReviewLeaseLost('lost')
   with patch('knowledge_v2.lease_heartbeat.renew_analysis_lease',side_effect=renewal),patch('knowledge_v2.check_runtime.execute',side_effect=operation),patch.object(bridge,'fail') as fail:
    with self.assertRaises(ReviewLeaseLost):bridge.once()
    fail.assert_not_called()
   journal=(bridge.store.directory/'lease-health'/(claim['command_id']+'.jsonl')).read_text()
   self.assertNotIn('private-lease',journal)
