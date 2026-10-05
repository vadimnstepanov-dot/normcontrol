import unittest
from unittest.mock import patch,Mock
from native_core import health

class GatewayHealthTests(unittest.TestCase):
    def test_gateway_health_has_no_windows_forwarder_dependency(self):
        client=Mock();client.json.return_value={'ready':True}
        with patch.object(health.Path,'read_text',return_value='{"token":"fixture","certificate":"cert"}'),patch.object(health,'Transport',return_value=client) as transport:
            health.check('gateway')
        transport.assert_called_once_with('https://127.0.0.1:8098','fixture','cert')
        client.json.assert_called_once_with('/core/health',timeout=4)
    def test_unready_gateway_is_not_reported_healthy(self):
        client=Mock();client.json.return_value={'ready':False}
        with patch.object(health.Path,'read_text',return_value='{"token":"fixture","certificate":"cert"}'),patch.object(health,'Transport',return_value=client),self.assertRaises(AssertionError):health.check('gateway')
