import io,json,unittest
from unittest.mock import Mock,patch
from nc5 import gateway


class ProfileGatewayTests(unittest.TestCase):
    def invoke(self,body,enabled=True,token='test-token',address='127.0.0.1'):
        cfg=dict(host='localhost',port=0,allowed_clients=['127.0.0.1'],token='test-token',certificate='fixture',private_key='fixture',allow_model_profiles=enabled)
        captured=[]
        def server(address,handler):captured.append(handler);return Mock()
        with patch.object(gateway,'read',return_value=cfg),patch.object(gateway,'ThreadingHTTPServer',side_effect=server),patch.object(gateway.ssl,'SSLContext'):
            gateway.serve('fixture')
        handler=object.__new__(captured[0]);raw=json.dumps(body).encode()
        handler.command='POST';handler.path='/model/profile';handler.client_address=(address,0)
        handler.headers={'Authorization':'Bearer '+token,'Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw)
        handler.send=Mock()
        with patch('llm_sidecar.ensure_profile',return_value={'profile':'vision','ready':True,'changed':True}) as switch:
            handler.dispatch();return handler.send.call_args.args[0],switch.call_count

    def test_disabled_by_default_and_authorization_precedes_switch(self):
        for options,status in [({'enabled':False},404),({'token':'wrong'},401),({'address':'foreign'},403)]:
            self.assertEqual(self.invoke({'profile':'vision'},**options),(status,0))

    def test_fixed_profiles_only(self):
        for body in ({'profile':'shell'},{'profile':'vision','command':'arbitrary'},['vision']):
            self.assertEqual(self.invoke(body),(400,0))
        self.assertEqual(self.invoke({'profile':'vision'}),(200,1))
