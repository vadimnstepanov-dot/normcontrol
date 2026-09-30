import io,json,unittest,http.client,threading,tempfile,uuid
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
from nc5 import gateway


class ProfileGatewayTests(unittest.TestCase):
    def test_shared_artifact_stream_has_exact_length_and_no_full_json_copy(self):
        cfg=dict(host='localhost',port=0,allowed_clients=['127.0.0.1'],token='test-token',certificate='fixture',private_key='fixture')
        captured=[]
        def server(address,handler):captured.append(handler);return Mock()
        with patch.object(gateway,'read',return_value=cfg),patch.object(gateway,'ThreadingHTTPServer',side_effect=server),patch.object(gateway.ssl,'SSLContext'):
            gateway.serve('fixture')
        pipeline=str(uuid.uuid4());value={'version':'pipeline-v1','pipeline':pipeline,'documents':[{'text':'fixture'}]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'artifact.json';path.write_text(json.dumps(value),encoding='utf8')
            handler=object.__new__(captured[0]);raw=json.dumps({'pipeline':pipeline}).encode()
            handler.command='POST';handler.path='/pipeline/artifact';handler.client_address=('127.0.0.1',0)
            handler.headers={'Authorization':'Bearer test-token','Content-Length':str(len(raw))};handler.rfile=io.BytesIO(raw);handler.wfile=io.BytesIO()
            handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock();handler.send=Mock()
            with patch('pipeline.host',return_value=Mock()),patch('pipeline.artifact_path',return_value=path),patch.object(gateway,'read',side_effect=AssertionError('Unexpected full artifact read')):
                handler.dispatch()
            handler.send_response.assert_called_once_with(200);handler.send.assert_not_called()
            body=handler.wfile.getvalue();self.assertEqual(json.loads(body),{'ready':True,**value})
            handler.send_header.assert_any_call('Content-Length',str(len(body)))

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

    def test_token_requests_keep_same_authenticated_http_connection(self):
        from unittest.mock import MagicMock
        ready=threading.Event();servers=[];addresses=[]
        cfg=dict(host='127.0.0.1',port=0,allowed_clients=['127.0.0.1'],token='test-token',certificate='fixture',private_key='fixture',upstream='http://unused')
        def create(address,handler):
            original=handler.dispatch
            def dispatch(self):addresses.append(self.client_address);return original(self)
            handler.dispatch=dispatch
            server=ThreadingHTTPServer(address,handler);servers.append(server);ready.set();return server
        upstream=MagicMock();upstream.__enter__.return_value=upstream;upstream.status=200;upstream.read.return_value=b'{"tokens":[1]}'
        context=Mock();context.wrap_socket.side_effect=lambda socket,**kw:socket
        with patch.object(gateway,'read',return_value=cfg),patch.object(gateway,'ThreadingHTTPServer',side_effect=create),patch.object(gateway.ssl,'SSLContext',return_value=context),patch.object(gateway.urllib.request,'urlopen',return_value=upstream):
            thread=threading.Thread(target=gateway.serve,args=('fixture',),daemon=True);thread.start();self.assertTrue(ready.wait(3))
            connection=http.client.HTTPConnection(*servers[0].server_address,timeout=3)
            try:
                for _ in range(2):
                    connection.request('POST','/tokenize',body=b'{}',headers={'Authorization':'Bearer test-token','Content-Type':'application/json'})
                    response=connection.getresponse();self.assertEqual(response.status,200);self.assertEqual(response.version,11);response.read()
                self.assertEqual(addresses[0],addresses[1])
            finally:connection.close();servers[0].shutdown();servers[0].server_close();thread.join(3)
