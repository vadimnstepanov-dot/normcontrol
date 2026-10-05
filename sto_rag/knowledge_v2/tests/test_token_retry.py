import io,json,unittest,urllib.error
from unittest.mock import patch,MagicMock
from knowledge_v2.review_client import LlamaClient

class TokenRetryTests(unittest.TestCase):
 def client(self):
  c=LlamaClient.__new__(LlamaClient);c.endpoint='https://gateway.test';return c
 def response(self,status,body):
  r=MagicMock();r.status=status;r.reason='gateway';r.headers={};r.read.return_value=json.dumps(body).encode();return r
 def test_template_and_tokenize_retry_gateway_once_with_identical_request(self):
  for path,body in (('/apply-template',{'prompt':'same'}),('/tokenize',{'tokens':[1,2]})):
   with self.subTest(path=path):
    connections=[MagicMock(),MagicMock()];connections[0].getresponse.return_value=self.response(503,{'error':'unavailable'});connections[1].getresponse.return_value=self.response(200,body)
    c=self.client()
    with patch('http.client.HTTPSConnection',side_effect=connections),patch('time.sleep') as sleep:
     self.assertEqual(c.token_http(path,{'messages':['private']},{'Authorization':'Bearer secret'},30),body)
    self.assertEqual(connections[0].request.call_args,connections[1].request.call_args)
    connections[0].close.assert_called_once();sleep.assert_called_once_with(1)
    self.assertIs(c._token_connection,connections[1])
 def test_errors_not_gateway_are_not_retried(self):
  for code in (400,401,403,409,422,500):
   conn=MagicMock();conn.getresponse.return_value=self.response(code,{})
   with self.subTest(code=code),patch('http.client.HTTPSConnection',return_value=conn) as factory,patch('time.sleep') as sleep:
    with self.assertRaises(urllib.error.HTTPError):self.client().token_http('/tokenize',{}, {},30)
    self.assertEqual(factory.call_count,1);sleep.assert_not_called()
 def test_repeated_outage_stops_after_two_requests(self):
  conns=[MagicMock(),MagicMock()]
  for conn in conns:conn.getresponse.return_value=self.response(503,{})
  with patch('http.client.HTTPSConnection',side_effect=conns) as factory,patch('time.sleep'):
   with self.assertRaises(urllib.error.HTTPError) as caught:self.client().token_http('/apply-template',{}, {},30)
   self.assertEqual(caught.exception.code,503);self.assertEqual(factory.call_count,2)
 def test_generation_is_never_retried(self):
  c=self.client();c.model='test';c.context=49152;c.output_tokens=4096;c.timeout=30
  error=urllib.error.HTTPError('https://gateway.test/v1/chat/completions',503,'down',{},io.BytesIO(b'{}'))
  with patch('knowledge_v2.review_client.urllib.request.urlopen',side_effect=error) as request:
   with self.assertRaises(urllib.error.HTTPError):c.http('/v1/chat/completions',{'messages':[]})
   self.assertEqual(request.call_count,1)
