import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from memory_recovery import MemoryRecovery

class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.sample={'total_mb':32000,'queue':[{'state':'waiting','resource':'gpu','ram_mb':512}]}
  self.q=SimpleNamespace(resource_snapshot=lambda:self.sample)
  self.r=MemoryRecovery(self.root/'state.json',self.q,self.root/'log',grace=300)
 def observe(self,now,**kwargs):
  with patch('pipeline.system_reserve_mb',return_value=2048):return self.r.observe(2100,False,True,now=now,**kwargs)
 def test_persistent_pressure_only_and_cooldown_survives_restart(self):
  self.assertIsNone(self.observe(1000));self.assertIsNotNone(self.observe(1300))
  r=MemoryRecovery(self.root/'state.json',self.q,self.root/'log',grace=0)
  with patch('pipeline.system_reserve_mb',return_value=2048):self.assertIsNone(r.observe(2100,False,True,now=1400))
 def test_busy_model_user_stop_and_running_gpu_are_never_restarted(self):
  with patch('pipeline.system_reserve_mb',return_value=2048):
   self.assertIsNone(self.r.observe(2100,True,True,now=1000))
   self.assertIsNone(self.observe(1000,stopped=True))
   self.sample['queue'].append({'state':'running','resource':'gpu','ram_mb':64})
   self.assertIsNone(self.observe(2000))
 def test_pressure_reset_and_available_memory(self):
  self.observe(1000)
  self.sample['queue']=[];self.assertIsNone(self.observe(1300))
  self.sample['queue']=[{'state':'waiting','resource':'gpu','ram_mb':512}]
  self.assertIsNone(self.observe(1400))
  with patch('pipeline.system_reserve_mb',return_value=2048):self.assertIsNone(self.r.observe(9000,False,True,now=2000))
 def test_recent_confirmed_oom_recovers_once_but_offline_alone_does_not(self):
  import time
  now=time.time();self.assertIsNone(self.r.observe(9000,False,False,now=now))
  (self.root/'log').write_text('CUDA error: out of memory',encoding='utf8')
  self.assertIsNotNone(self.r.observe(9000,False,False,now=now+1))
  self.assertIsNone(self.r.observe(9000,False,False,now=now+2))

if __name__=='__main__':unittest.main()
