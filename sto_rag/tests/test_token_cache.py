import tempfile,unittest,json
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
from types import SimpleNamespace
from token_cache import TokenCache,key
from nc5.model import Client
from nc5.store import Store
from nc5.common import config
from knowledge_v2.store import KnowledgeStore,checksum
from knowledge_v2.review_client import LlamaClient

class TokenCacheTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.native=Store(Path(self.tmp.name)/'native.sqlite3');self.norm=KnowledgeStore(Path(self.tmp.name)/'norm')
 def test_old_normative_measurements_remain_exactly_reusable(self):
  req={'messages':[{'content':'Текст с условиями'}]};cache=TokenCache(self.norm.connection)
  self.assertEqual(key('model-template',req),checksum(['model-template',req]))
  cache.put('model-template',req,123)
  self.assertEqual(TokenCache(self.norm.connection).get('model-template',req),123)
  self.assertIsNone(cache.get('changed-model',req));self.assertIsNone(cache.get('model-template',{'messages':[]}))
 def test_missing_identity_and_invalid_counts_never_cached(self):
  cache=TokenCache(self.native.connect)
  for n in (-1,True,1.2,None):cache.put('s',{},n)
  cache.put('',{},3);self.assertIsNone(cache.get('s',{}));self.assertIsNone(cache.get('',{}))
 def test_persistent_cache_is_bounded(self):
  cache=TokenCache(self.native.connect);cache.put('s',{},0)
  with self.native.connect() as db:db.executemany('INSERT INTO token_measurements VALUES(?,?,?)',((str(i),i,i) for i in range(41000)))
  cache.writes=999;cache.put('s',{},4)
  with self.native.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM token_measurements').fetchone()[0],40000)
 def wire(self,calls):
  def http(path,value,*args):
   calls.append(path)
   return {'prompt':json.dumps(value,ensure_ascii=False)} if path=='/apply-template' else {'tokens':list(range(len(value['content'])))}
  return http
 def nc(self):
  c=Client(config(),store=self.native);c.signature='model-A';return c
 def norm_client(self):
  c=LlamaClient.__new__(LlamaClient);c.signature='model-A';c._counts={};c._schema_counts={};c.store=self.norm
  c.request=lambda p:dict(messages=[{'content':p['text']}],response_format={'type':'json_schema','schema':p.get('schema','same')})
  return c
 def test_native_memory_and_restart_cache_bypass_gpu_and_http(self):
  p={'stage':'language','blocks':[]};c=self.nc();calls=[];c.http=self.wire(calls);expected=c.count(p)
  self.assertEqual(len(calls),3);c.config['pipeline_id']='pipeline'
  with patch('pipeline.host',side_effect=AssertionError('Cached count must not join GPU queue')):
   c.http=lambda *a,**k:self.fail('Cached count called model');self.assertEqual(c.count(p),expected)
   restarted=self.nc();restarted.config['pipeline_id']='pipeline';restarted.http=c.http
   self.assertEqual(restarted.count(p),expected)
 def test_normative_memory_and_restart_cache_bypass_gpu_and_http(self):
  p={'text':'Требование с условием'};c=self.norm_client();calls=[];c.http=self.wire(calls);expected=c.count(p)
  self.assertEqual(len(calls),3);c.pipeline_id='pipeline'
  with patch('pipeline.remote_turn',side_effect=AssertionError('Cached count must not join GPU queue')):
   c.http=lambda *a,**k:self.fail('Cached count called model');self.assertEqual(c.count(p),expected)
   restarted=self.norm_client();restarted.pipeline_id='pipeline';restarted.http=c.http
   self.assertEqual(restarted.count(p),expected)
 def test_native_schema_cache_and_model_change(self):
  c=self.nc();calls=[];c.http=self.wire(calls)
  a=c.count({'stage':'language','blocks':[]});c.count({'stage':'language','blocks':[],'context':'Другой текст'})
  self.assertEqual(len(calls),5)
  c.signature='model-B';self.assertEqual(c.count({'stage':'language','blocks':[]}),a);self.assertEqual(len(calls),8)
 def test_normative_schema_and_model_changes_force_new_measurement(self):
  c=self.norm_client();calls=[];c.http=self.wire(calls)
  c.count({'text':'a'});c.count({'text':'b'});self.assertEqual(len(calls),5)
  c.count({'text':'a','schema':'new'});self.assertEqual(len(calls),8)
  c.signature='model-B';c.count({'text':'a'});self.assertEqual(len(calls),11)
 def test_visual_reserve_is_part_of_persistent_budget_identity(self):
  c=self.nc();req={'messages':['same image wire']}
  self.assertNotEqual(c.measurement_request({'images':[{'tokens_reserve':100}]},req),c.measurement_request({'images':[{'tokens_reserve':200}]},req))
 def test_memory_cache_sizes_are_bounded(self):
  c=self.nc();n=self.norm_client()
  for i in range(2100):c._remember_count(str(i),i);n._remember_count(str(i),i)
  self.assertLessEqual(len(c.count_cache),2000);self.assertLessEqual(len(n._counts),1024)

if __name__=='__main__':unittest.main()
