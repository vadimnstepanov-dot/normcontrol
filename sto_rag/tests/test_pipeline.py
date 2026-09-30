import tempfile
import unittest
import json
import io
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from pipeline import Coordinator,cache_budget,preparation_mb,remote_turn,system_reserve_mb


class PipelineTests(unittest.TestCase):
 def setUp(self):
  self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
  self.available=16000
  self.q=Coordinator(Path(self.folder.name)/'queue.sqlite3',memory_probe=lambda:(32000,self.available))
 def test_gpu_exclusion_and_cpu_parallelism(self):
  gpu=self.q.ticket('a','language','gpu')
  other=self.q.ticket('a','normative','gpu')
  cpu=self.q.ticket('a','parse','cpu')
  self.assertEqual(gpu['state'],'running');self.assertEqual(other['state'],'waiting');self.assertEqual(cpu['state'],'running')
  self.q.release(gpu['id'])
  self.assertEqual(self.q.ticket('a','normative','gpu',other['id'])['state'],'running')
 def test_cpu_capacity(self):
  self.assertEqual(self.q.ticket('a','one','cpu')['state'],'running')
  self.assertEqual(self.q.ticket('a','two','cpu')['state'],'running')
  self.assertEqual(self.q.ticket('a','three','cpu')['state'],'waiting')
 def test_ram_pressure_blocks_and_recovers(self):
  self.available=2500
  ticket=self.q.ticket('a','parse','cpu',ram_mb=1024)
  self.assertEqual(ticket['state'],'waiting')
  self.available=6000
  self.assertEqual(self.q.ticket('a','parse','cpu',ticket['id'],1024)['state'],'running')
 def test_cross_resource_reservations(self):
  self.available=6348
  self.assertEqual(self.q.ticket('a','parse','cpu',ram_mb=2048)['state'],'running')
  self.assertEqual(self.q.ticket('a','llm','gpu')['state'],'waiting')
 def test_impossible_task_fails_without_queue_entry(self):
  with self.assertRaises(MemoryError):self.q.ticket('a','parse','cpu',ram_mb=30000)
  self.assertEqual(self.q.resources('a')['queue'],[])
 def test_identity_and_scoped_release(self):
  ticket=self.q.ticket('a','parse','cpu')
  self.q.release(ticket['id'],'b')
  with self.assertRaises(ValueError):self.q.ticket('b','parse','cpu',ticket['id'])
  with self.assertRaises(ValueError):self.q.ticket('a','parse','cpu',ticket['id'],1024)
  self.assertEqual(len(self.q.resources('a')['queue']),1)
 def test_expired_ticket_does_not_block(self):
  ticket=self.q.ticket('a','old','gpu')
  with self.q.db() as db:db.execute('UPDATE tickets SET expires=0 WHERE id=?',(ticket['id'],))
  self.assertEqual(self.q.ticket('a','new','gpu')['state'],'running')
 def test_normative_preparation_does_not_depend_on_language(self):
  self.q.register('a',['language','logic','sto'])
  with self.assertRaises(ValueError):self.q.phase('a','normative','running')
  self.q.phase('a','material','done');self.q.phase('a','normative_prepare','running');self.q.phase('a','normative_prepare','done')
  self.q.phase('a','normative','running')
  self.assertEqual({r['stage']:r['state'] for r in self.q.status('a')}['language'],'waiting')
 def test_turn_releases_after_failure(self):
  with self.assertRaises(RuntimeError):
   with self.q.turn('a','parse','cpu'):raise RuntimeError('fixture')
  self.assertEqual(self.q.resources('a')['queue'],[])
 def test_vision_waits_for_native_profile_owner(self):
  self.q.register('a',['language','sto'])
  self.q.phase('a','material','done');self.q.phase('a','normative_prepare','done');self.q.phase('a','normative','done')
  with self.assertRaises(ValueError):self.q.phase('a','vision','running')
  self.q.phase('a','verify_native','done');self.q.phase('a','vision','running')
 def test_cache_under_pressure_disabled_and_size_bounded(self):
  with patch('pipeline.memory_mb',return_value=(32000,2700)):self.assertEqual(cache_budget(8*1024**3),0)
  with patch('pipeline.memory_mb',return_value=(32000,16000)):self.assertEqual(cache_budget(8*1024**3),1024*1048576)
 def test_requested_four_gib_reserve_is_fixed(self):
  self.assertEqual(system_reserve_mb(32000),4096)
  self.assertEqual(system_reserve_mb(16000),4096)
  self.available=4096
  self.assertEqual(self.q.ticket('a','parse','cpu',ram_mb=64)['state'],'waiting')
  self.assertEqual(self.q.resources('a')['reserve_mb'],4096)
 def test_file_size_reservation(self):
  path=Path(self.folder.name)/'fixture.docx';path.write_bytes(b'fixture')
  self.assertGreaterEqual(preparation_mb([path]),512)
 def test_disabled_cache_does_not_allocate_serialized_copy(self):
  from nc5.store import LRU
  cache=LRU(0)
  with patch('nc5.store.dumps',side_effect=AssertionError('Unexpected serialization')):cache.put('a',{'fixture':True})
  self.assertEqual(cache.size,0);self.assertIsNone(cache.get('a'))

 def transport(self,request,**kwargs):
  payload=json.loads(request.data)
  self.assertEqual(request.get_header('Authorization'),'Bearer fixture-token')
  if payload['action']=='release':self.q.release(payload['id'],payload['pipeline']);result={'released':True}
  else:result=self.q.ticket(payload['pipeline'],payload['stage'],payload['resource'],payload.get('id'),payload['ram_mb'])
  return io.BytesIO(json.dumps(result).encode())

 def test_remote_cpu_does_not_claim_model_ticket(self):
  client=SimpleNamespace(pipeline_id='a',pipeline_endpoint='http://fixture',timeout=1)
  with patch.dict('os.environ',{'NORMCONTROL_LLM_API_KEY':'fixture-token'}),patch('urllib.request.urlopen',side_effect=self.transport):
   with remote_turn(client,'plan','cpu'):
    self.assertFalse(getattr(client,'_model_ticket',None))
    with remote_turn(client,'count',ram_mb=64):self.assertTrue(client._model_ticket)
    self.assertIsNone(client._model_ticket)
  self.assertEqual(self.q.resources('a')['queue'],[])

 def test_remote_failure_releases_gpu(self):
  client=SimpleNamespace(pipeline_id='a',pipeline_endpoint='http://fixture',timeout=1,_model_ticket=None)
  with patch.dict('os.environ',{'NORMCONTROL_LLM_API_KEY':'fixture-token'}),patch('urllib.request.urlopen',side_effect=self.transport):
   with self.assertRaises(RuntimeError):
    with remote_turn(client):raise RuntimeError('fixture')
  self.assertIsNone(client._model_ticket);self.assertEqual(self.q.resources('a')['queue'],[])

 def test_lightweight_count_can_run_inside_cpu_reservation(self):
  self.available=4748
  with self.q.turn('a','parse','cpu'):
   with self.q.turn('a','count','gpu',ram_mb=64):pass
  self.assertEqual(self.q.resources('a')['queue'],[])

 def test_shared_conversion_preserves_original_source_identity(self):
  from zipfile import ZipFile
  from knowledge_v2.review import corpus
  from knowledge_v2.ingest import sha256
  source=Path(self.folder.name)/'original.doc';source.write_bytes(b'original-source-fixture')
  converted=Path(self.folder.name)/'converted.docx'
  with ZipFile(converted,'w') as archive:
   archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Requirement fixture 42</w:t></w:r></w:p></w:body></w:document>')
  documents=corpus([source],prepared_paths={str(source.resolve()):converted})
  self.assertEqual(documents[0]['sha256'],sha256(source));self.assertEqual(documents[0]['name'],'original.doc')
  self.assertIn('Requirement fixture 42',[block['text'] for block in documents[0]['blocks']])

 def test_ram_waiter_cannot_deadlock_cpu_token_probe(self):
  self.available=4748
  with self.q.turn('a','plan','cpu'):
   heavy=self.q.ticket('a','normative','gpu')
   self.assertEqual(heavy['state'],'waiting')
   with self.q.turn('a','count','gpu',ram_mb=64):pass
  self.assertEqual(self.q.ticket('a','normative','gpu',heavy['id'])['state'],'running')

if __name__=='__main__':unittest.main()
