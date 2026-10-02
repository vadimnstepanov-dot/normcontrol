import json,tempfile,unittest,uuid,sys,importlib
from pathlib import Path
from unittest.mock import patch
from native_core.resources import effective_available
from pipeline import Coordinator

class BudgetTests(unittest.TestCase):
    def test_model_restart_waits_for_profile_ticket(self):
        from native_core.windows_monitor import gpu_idle,wait_gpu
        profile={'queue':[{'state':'running','resource':'gpu'}]}
        self.assertFalse(gpu_idle(profile))
        self.assertTrue(gpu_idle({'queue':[{'state':'waiting','resource':'gpu'},{'state':'running','resource':'cpu'}]}))
        with self.assertRaises(TimeoutError):wait_gpu(lambda route,value:profile,timeout=-1)
        wait_gpu(lambda route,value:{'queue':[]},timeout=0)
    def test_both_memory_limits(self):
        self.assertEqual(effective_available(4700,5000),4700)
        self.assertEqual(effective_available(4700,700),2236)
        self.assertEqual(effective_available(1500,7000),1500)
    def test_cpu_gpu_parallel_and_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=Coordinator(Path(directory)/'queue.db',memory_probe=lambda:(16000,4096))
            key=str(uuid.uuid4());cpu=queue.ticket(key,'material','cpu',ram_mb=1024)
            gpu=queue.ticket(key,'language','gpu',ram_mb=64)
            second=queue.ticket(str(uuid.uuid4()),'chat','gpu',ram_mb=64)
            heavy=queue.ticket(key,'another','cpu',ram_mb=1024)
            self.assertEqual((cpu['state'],gpu['state'],second['state'],heavy['state']),('running','running','waiting','waiting'))
            queue.release(gpu['id']);self.assertEqual(queue.ticket(key,'another','cpu',heavy['id'],1024)['state'],'running')
            queue.release(heavy['id']);self.assertEqual(queue.ticket(str(uuid.uuid4()),'test','cpu',ram_mb=64)['state'],'running')
    def test_restart_keeps_queue_and_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'queue.db';key=str(uuid.uuid4())
            queue=Coordinator(path,memory_probe=lambda:(16000,4096));queue.register(key,['language','logic','sto'])
            with self.assertRaises(ValueError):queue.phase(key,'normative','running')
            queue.phase(key,'material','done');ticket=queue.ticket(key,'language','gpu',ram_mb=64)
            reopened=Coordinator(path,memory_probe=lambda:(16000,4096))
            self.assertEqual(reopened.ticket(key,'language','gpu',ticket['id'],64)['state'],'running')
            self.assertIn('normative_prepare',[r['stage'] for r in reopened.status(key)])

class CheckpointTests(unittest.TestCase):
    def test_does_not_resume_later_user_pause(self):
        from nc5 import common,store
        with tempfile.TemporaryDirectory() as directory,patch.object(common,'DATA',Path(directory)):
            importlib.reload(store)
            import native_core.checkpoint as checkpoint
            importlib.reload(checkpoint)
            db=store.Store();a=db.create({});b=db.create({})
            for job in (a,b):
                with db.connect() as connection:connection.execute("UPDATE jobs SET state='running' WHERE id=?",(job,))
            marker=Path(directory)/'checkpoint.json';token,ids=checkpoint.pause(path=marker)
            db.event(b,'state',{'state':'paused'})
            self.assertEqual(checkpoint.resume(token,marker),[a])
            self.assertEqual(db.job(b)['state'],'paused')
        importlib.reload(store);importlib.reload(checkpoint)

if __name__=='__main__':unittest.main()
