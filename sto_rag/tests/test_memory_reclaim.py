import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from pipeline import Coordinator,system_reserve_mb
from memory_reclaim import Reclaimer

class ReclaimTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.available=system_reserve_mb(32000)+128
  self.q=Coordinator(Path(self.tmp.name)/'queue',memory_probe=lambda:(32000,self.available))
  self.policy=Path(self.tmp.name)/'policy.json';self.policy.write_text(json.dumps({'wsl_cache_reclaim_distro':'NormControl'}))
  self.r=Reclaimer(self.policy,self.q)
 def test_only_idle_model_and_ram_wait_trigger_cache_reclaim(self):
  self.q.ticket('p','norm','gpu')
  with patch('memory_reclaim.os.name','nt'),patch('memory_reclaim.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
   self.assertIsNone(self.r.check(self.available,True));self.assertIsNone(self.r.check(self.available,None))
   self.assertTrue(self.r.check(self.available,False));self.assertIsNone(self.r.check(self.available,False))
   run.assert_called_once();args=run.call_args.args[0];self.assertEqual(args[:6],['wsl.exe','-d','NormControl','-u','root','--'])
   self.assertIn('drop_caches',args[-1]);self.assertNotIn('shutdown',args)
 def test_running_gpu_missing_opt_in_and_sufficient_ram_do_not_reclaim(self):
  self.q.ticket('p','norm','gpu')
  with patch('memory_reclaim.os.name','nt'),patch('memory_reclaim.subprocess.run') as run:
   self.assertIsNone(self.r.check(8048,False))
   self.policy.unlink();self.assertIsNone(self.r.check(self.available,False))
   self.policy.write_text(json.dumps({'wsl_cache_reclaim_distro':'bad; distro'}));self.assertIsNone(self.r.check(self.available,False));run.assert_not_called()
 def test_running_gpu_prevents_reclaim(self):
  self.q.memory_probe=lambda:(32000,8048);self.q.ticket('p','norm','gpu');self.q.ticket('p','other','gpu')
  with patch('memory_reclaim.os.name','nt'),patch('memory_reclaim.subprocess.run') as run:
   self.assertIsNone(self.r.check(self.available,False));run.assert_not_called()
 def test_wsl_failure_does_not_crash_monitor(self):
  self.q.ticket('p','norm','gpu')
  with patch('memory_reclaim.os.name','nt'),patch('memory_reclaim.subprocess.run',side_effect=OSError('unavailable')):
   self.assertFalse(self.r.check(self.available,False))
 def test_linux_queue_snapshot_avoids_windows_database(self):
  remote=SimpleNamespace(resource_snapshot=lambda:{'total_mb':32000,'queue':[{'state':'waiting','resource':'gpu','ram_mb':512}]})
  reclaimer=Reclaimer(self.policy,remote)
  with patch('memory_reclaim.os.name','nt'),patch('memory_reclaim.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
   self.assertTrue(reclaimer.check(self.available,False));run.assert_called_once()

if __name__=='__main__':unittest.main()
