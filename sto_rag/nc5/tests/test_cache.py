import copy
import unittest
from unittest.mock import patch
from nc5.common import DEFAULT_CONFIG
from nc5.engine import response_cache_key
from nc5.model import Client


class CacheTests(unittest.TestCase):
    def test_exact_request_inputs_invalidate_cache(self):
        client=Client(dict(DEFAULT_CONFIG));client.signature='model-and-template-A'
        payload={'stage':'logic','profile':{'type':'ТЗ'},'blocks':[
            {'document':'sha-1','locator':'p9','text':'Объём 3 ГБ.',
             'path':['2 Требования'],'table':{'table':1,'row':2,'column':1,'column_name':'Объём'}}],
             'requirements':[{'requirement_id':'rule-v1','source_quote':'Указать объём.'}]}
        key=response_cache_key(client,payload)
        for field,value in [('text','Объём 4 ГБ.'),('document','sha-2'),('path',['3 Ограничения'])]:
            changed=copy.deepcopy(payload);changed['blocks'][0][field]=value
            self.assertNotEqual(key,response_cache_key(client,changed))
        changed=copy.deepcopy(payload);changed['blocks'][0]['table']['column_name']='Пример объёма'
        self.assertNotEqual(key,response_cache_key(client,changed))
        changed=copy.deepcopy(payload);changed['requirements'][0]['source_quote']='Рекомендуется указать объём.'
        self.assertNotEqual(key,response_cache_key(client,changed))
        with patch('nc5.model.stage_policy',return_value='different policy'):
            self.assertNotEqual(key,response_cache_key(client,payload))
        client.signature='model-and-template-B'
        self.assertNotEqual(key,response_cache_key(client,payload))

    def test_non_request_settings_do_not_discard_immutable_response(self):
        c=Client(dict(DEFAULT_CONFIG));c.signature='model'
        p={'stage':'language','blocks':[]};key=response_cache_key(c,p)
        c.config['ram_bytes']=1;c.config['port']=9999
        self.assertEqual(key,response_cache_key(c,p))


if __name__=='__main__':unittest.main()
