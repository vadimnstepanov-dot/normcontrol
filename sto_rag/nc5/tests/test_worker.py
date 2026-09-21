import os
import unittest
from unittest.mock import patch

from nc5.worker import runtime_config


class WorkerConfigurationTests(unittest.TestCase):
    @patch('nc5.worker.config')
    def test_portal_cannot_replace_local_planning_tuning(self, local_config):
        local_config.return_value={
            'endpoint':'http://127.0.0.1:8082',
            'language_chunk_chars':14000,
            'logic_chunk_chars':12000,
            'reference_group_size':3,
            'verification_group_size':3,
            'sto_group_size':4,
            'context':20480,
        }
        remote={
            'endpoint':'http://remote.invalid:8098',
            'context':24576,
            'language_chunk_chars':6500,
            'verification_group_size':1,
        }
        with patch.dict(os.environ,{},clear=True):
            merged=runtime_config(remote)
        self.assertEqual(merged['context'],24576)
        self.assertEqual(merged['endpoint'],'http://127.0.0.1:8082')
        self.assertEqual(merged['language_chunk_chars'],14000)
        self.assertEqual(merged['verification_group_size'],3)

    @patch('nc5.worker.config')
    def test_remote_endpoint_is_opt_in(self, local_config):
        local_config.return_value={'endpoint':'http://127.0.0.1:8082'}
        with patch.dict(os.environ,{'NORMCONTROL_REMOTE_LLM_CONFIG':'1'},clear=True):
            merged=runtime_config({'endpoint':'http://127.0.0.1:8098'})
        self.assertEqual(merged['endpoint'],'http://127.0.0.1:8098')


if __name__=='__main__':
    unittest.main()
