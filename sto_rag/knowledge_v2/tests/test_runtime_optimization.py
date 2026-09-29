import json,tempfile,uuid,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from knowledge_v2.check_log import Journal,compact_plan,expand_plan
from knowledge_v2.store import checksum
from knowledge_v2.review_wire import payload
class OptimizationTests(unittest.TestCase):
 def test_bounded_log_delivery_preserves_every_unicode_part_and_single_event_hash(self):
  with tempfile.TemporaryDirectory() as tmp:
   journal=Journal(Path(tmp),str(uuid.uuid4()));journal.append('plan',{'text':'Данные'*110000})
   sent=[];claim={'command_id':'command','lease':'lease'};bridge=SimpleNamespace(transport=lambda path,row:sent.append(row))
   with patch('knowledge_v2.check_log.checksum',wraps=checksum) as measure:journal.deliver(bridge,claim,max_chunks=1)
   self.assertEqual(len(sent),1);self.assertEqual(measure.call_count,2)
   journal.deliver(bridge,claim);before=len(sent);journal.deliver(bridge,claim);self.assertEqual(len(sent),before)
   entries=[e for chunk in sent for e in chunk['entries']];self.assertEqual([e['part'] for e in entries],list(range(len(entries))))
   restored=json.loads(''.join(e['text'] for e in entries));self.assertEqual(restored['value']['text'],'Данные'*110000)
   self.assertEqual(checksum(restored),entries[0]['sha256'])
 def test_plan_references_reconstruct_identical_packets(self):
  blocks=[{'id':'b1','text':'Условие','table':1,'row':3,'header_path':['Параметр']}];rows=[{'id':'r1','atom':{'conditions':['Если применимо'],'exceptions':['Кроме теста']}}]
  p={'documents':[{'blocks':blocks}],'rows':rows,'batches':[{'id':'packet','payload':{'documents':blocks,'obligations':rows,'stage':'check','completeness':{'full_text':True,'expected_ids':['b1']}}}]}
  q=compact_plan(p);self.assertEqual(expand_plan(q),p)

 def test_read_only_token_transport_reuses_connection(self):
  from knowledge_v2.review_client import LlamaClient
  client=LlamaClient.__new__(LlamaClient);client.endpoint='http://trusted.local/base'
  connection=SimpleNamespace(request=lambda *a,**k:None,getresponse=lambda:SimpleNamespace(status=200,read=lambda:b'{"tokens":[1,2]}'),close=lambda:None)
  with patch('http.client.HTTPConnection',return_value=connection) as create:
   self.assertEqual(client.token_http('/tokenize',{'content':'a'},{},3)['tokens'],[1,2])
   self.assertEqual(client.token_http('/tokenize',{'content':'b'},{},3)['tokens'],[1,2])
   self.assertEqual(create.call_count,1)
 def test_schema_token_cache_reuses_only_identical_schema(self):
  from knowledge_v2.review_client import LlamaClient
  client=LlamaClient.__new__(LlamaClient);client._counts={};client._schema_counts={};calls=[]
  client.request=lambda p:{'messages':[{'content':p['text']}],'response_format':{'type':'json_schema','schema':p.get('schema','same')}}
  def http(path,value):
   calls.append((path,value))
   return {'prompt':value['messages'][0]['content']} if path=='/apply-template' else {'tokens':list(range(len(value['content'])))}
  client.http=http
  first=client.count({'text':'first'});second=client.count({'text':'second'})
  self.assertEqual(second-first,1)
  self.assertEqual(sum(path=='/tokenize' for path,value in calls),3)
  client.count({'text':'third','schema':'changed'})
  self.assertEqual(sum(path=='/tokenize' for path,value in calls),5)
 def test_dedicated_review_role_cannot_be_disabled_by_inherited_skip_flag(self):
  from knowledge_v2.bridge import Bridge
  import os
  for dedicated,expected in (('0',False),('1',True)):
   messages=[]
   bridge=Bridge(SimpleNamespace(pending_delivery=lambda:None),'https://portal.example','x'*32,
    transport=lambda path,row:(messages.append(row) or {'command':None}))
   with patch.dict(os.environ,{'KNOWLEDGE_SKIP_REVIEW':'1','KNOWLEDGE_REVIEW_ONLY':dedicated,'KNOWLEDGE_EXPERT_ONLY':'0'}):
    bridge.once()
   self.assertEqual('review.execute' in messages[0]['capabilities'],expected)
if __name__=='__main__':unittest.main()
