import unittest
from unittest.mock import patch
import llm_sidecar


class ModelAutostartTests(unittest.TestCase):
    def test_queue_start_never_resumes_paused_checks(self):
        remembered=['paused-review']
        with patch.object(llm_sidecar,'Lease'),patch.object(llm_sidecar,'SleepInhibitor'),\
                patch.object(llm_sidecar,'save_state'),patch.object(llm_sidecar,'start_backend') as start,\
                patch.object(llm_sidecar,'ready',return_value=True),patch.object(llm_sidecar,'control') as control:
            result=llm_sidecar.execute_model_command({'action':'start','automatic':True},None,remembered)
        start.assert_called_once_with()
        control.assert_not_called()
        self.assertEqual(result,remembered)

    def test_manual_control_keeps_existing_checkpoint_behavior(self):
        with patch.object(llm_sidecar,'control',return_value=[]) as control:
            self.assertEqual(llm_sidecar.execute_model_command({'action':'start'},None,['paused']),[])
        control.assert_called_once_with('start',None,['paused'])

    def test_not_ready_model_does_not_acknowledge_success(self):
        with patch.object(llm_sidecar,'Lease'),patch.object(llm_sidecar,'SleepInhibitor'),\
                patch.object(llm_sidecar,'save_state'),patch.object(llm_sidecar,'start_backend'),\
                patch.object(llm_sidecar,'ready',return_value=False):
            with self.assertRaises(RuntimeError):
                llm_sidecar.execute_model_command({'action':'start','automatic':True},None,[])
