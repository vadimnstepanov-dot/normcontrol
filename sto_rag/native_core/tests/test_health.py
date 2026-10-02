import json
import unittest
from unittest.mock import patch

from native_core.health import check


class GatewayHealthTests(unittest.TestCase):
    def test_uses_installation_endpoint_instead_of_workstation_address(self):
        config = {'token': 'synthetic-test-token', 'certificate': '/test/gateway.crt'}
        with patch.dict('os.environ', NORMCONTROL_QUEUE_ENDPOINT='https://test-gateway:8098'), \
                patch('native_core.health.Path.read_text', return_value=json.dumps(config)), \
                patch('native_core.health.Transport') as transport:
            transport.return_value.json.return_value = {'ready': True}
            check('gateway')
        transport.assert_called_once_with('https://test-gateway:8098', config['token'], config['certificate'])
        transport.return_value.json.assert_called_once_with('/core/health', timeout=4)
