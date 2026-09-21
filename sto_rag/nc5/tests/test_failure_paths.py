import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nc5.store import Store
from nc5.model import Client,OutputError,SCHEMA
from nc5.common import config

class Failures(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        isolated=patch('nc5.common.DATA',Path(tmp.name));isolated.start();self.addCleanup(isolated.stop)
    def test_truncated_response_is_not_success(self):
        c=Client(config());c.count=lambda p:100;c.http=lambda *a,**kw:{'choices':[{'finish_reason':'length','message':{'content':'{"findings":[]}'}}]}
        with self.assertRaises(OutputError):c.generate({'stage':'language'})
    def test_malformed_response_is_not_success(self):
        c=Client(config());c.count=lambda p:100;c.http=lambda *a,**kw:{'choices':[{'finish_reason':'stop','message':{'content':'{"findings":[]}'}}]}
        with self.assertRaises(OutputError):c.generate({'stage':'language'})
    def test_pause_claim_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'db');j=s.create({});s.add(j,'logic',{'document':'a'});s.update(j,'paused');self.assertIsNone(s.claim(j,'logic'));s.update(j,'running');t=s.claim(j,'logic');self.assertIsNotNone(t);s.recover();self.assertEqual(s.job(j)['state'],'paused');self.assertEqual(s.tasks(j)[0]['state'],'pending')
    def test_table_context_factored_without_loss(self):
        c=Client(config());payload={'stage':'language','blocks':[{'document':'d','locator':'p1','text':'a','table':{'table':1,'row':2,'column':1,'column_name':'Имя атрибута','title':'Таблица А'}},{'document':'d','locator':'p2','text':'b','table':{'table':1,'row':3,'column':1,'column_name':'Имя атрибута','title':'Таблица А'}}]}
        req=c.request(payload);import json;p=json.loads(req['messages'][1]['content']);self.assertEqual(p['table_context']['d:1']['columns']['1'],'Имя атрибута');self.assertEqual(p['blocks'][1]['table']['row'],3);self.assertIn('column_name',payload['blocks'][0]['table'])

if __name__=='__main__':unittest.main()
