import unittest
from nc5.worker import probe_required

class WorkerRecoveryTests(unittest.TestCase):
    def test_startup_probes_even_with_orphaned_running_job(self):
        self.assertTrue(probe_required(True,False,0,20))
    def test_healthy_active_job_does_not_run_extra_model_probes(self):
        self.assertFalse(probe_required(True,True,0,20))
    def test_idle_worker_probes_at_existing_interval(self):
        self.assertTrue(probe_required(False,True,0,20))
        self.assertFalse(probe_required(False,True,10,20))
