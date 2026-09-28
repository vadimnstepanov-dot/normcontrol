import json,os,uuid,io,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from django.test import TestCase
from django.contrib.auth.models import User
from portal.models import Batch,WorkerRun,BatchLogChunk
from .check_log import stream_events,workbook,validate_chunk
from .services import digest
from knowledge_v2.check_log import Journal,clean

class JournalTests(TestCase):
    def setUp(self):
        self.u=User.objects.create_user('journal-owner')
        self.batch=Batch.objects.create(owner=self.u,name='Journal test',logging_enabled=True)
        self.run=WorkerRun.objects.create(batch=self.batch,worker='test')
        self.client.force_login(self.u)
    def chunk(self,event,sequence=0):
        entries=[dict(event_id=event['id'],kind=event['kind'],part=0,total=1,text=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':')),sha256=digest(event))]
        return dict(sequence=sequence,entries=entries,digest=digest(entries))
    def event(self):return dict(id=str(uuid.uuid4()),kind='result',at=123,value={'password':'PRIVATE','note':'=1+1','text':'длинный 😀'*7000})
    def test_receive_authenticated_checksum_and_replay(self):
        token='test-only-journal-token-123456789123456789'
        data=self.chunk(dict(id='result',kind='result',at=123,value={'done':True}))
        url=f'/normcontol/worker/{self.run.lease}/log/'
        with patch.dict(os.environ,NORMCONTROL_WORKER_TOKEN=token):
            r=self.client.post(url,json.dumps(data),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+token);self.assertEqual(r.status_code,200)
            r=self.client.post(url,json.dumps(data),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+token);self.assertEqual(r.status_code,200)
            changed=self.chunk(dict(id='other',kind='result',at=1,value={}))
            r=self.client.post(url,json.dumps(changed),content_type='application/json',HTTP_AUTHORIZATION='Bearer '+token);self.assertEqual(r.status_code,409)
        self.assertEqual(BatchLogChunk.objects.count(),1)
    def test_spool_replay_is_append_stable_and_full_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp),str(uuid.uuid4()));journal.append('result',self.event()['value'])
            sent=[]
            bridge=SimpleNamespace(transport=lambda path,d:sent.append(d))
            claim={'command_id':str(uuid.uuid4()),'lease':str(uuid.uuid4())}
            journal.deliver(bridge,claim);before=len(sent)
            journal.deliver(bridge,claim);self.assertEqual(len(sent),before)
            journal.append('result',{'done':True});journal.deliver(bridge,claim)
            for row in sent:BatchLogChunk.objects.create(batch=self.batch,**{k:row[k] for k in ('sequence','entries','digest')})
            events=list(stream_events(self.batch.log_chunks));self.assertEqual(len(events),2)
            self.assertEqual(events[0]['value']['password'],'[СКРЫТО]');self.assertEqual(events[0]['value']['text'],self.event()['value']['text'])
            self.batch.status='completed';self.batch.save()
            response=self.client.get(f'/normcontol/batches/{self.batch.pk}/log.xlsx');self.assertEqual(response.status_code,200)
            content=b''.join(response.streaming_content);response.close()
            with ZipFile(io.BytesIO(content)) as z:
                texts=''.join(z.read(n).decode('utf8') for n in z.namelist() if n.startswith('xl/worksheets'))
                self.assertNotIn('PRIVATE',texts);self.assertNotIn('<f>',texts);self.assertIn('=1+1',texts)
                ns={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                doc=ET.fromstring(z.read('xl/worksheets/sheet9.xml'))
                pieces=[e.text or '' for e in doc.findall('.//x:row/x:c[last()]/x:is/x:t',ns)][1:]
                self.assertEqual(''.join(pieces),self.event()['value']['text'])
    def test_gap_and_unbounded_part_rejected(self):
        data=self.chunk(dict(id='result',kind='result',at=123,value={}),sequence=1)
        BatchLogChunk.objects.create(batch=self.batch,**data)
        with self.assertRaises(ValueError):list(stream_events(self.batch.log_chunks))
        data['entries'][0]['part']=-1;data['digest']=digest(data['entries'])
        with self.assertRaises(ValueError):validate_chunk(data)
    def test_other_user_cannot_export(self):
        other=User.objects.create_user('other-reader');self.client.force_login(other)
        self.assertEqual(self.client.get(f'/normcontol/batches/{self.batch.pk}/log.xlsx').status_code,404)

    def test_partial_delivery_resume_with_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            job=str(uuid.uuid4());journal=Journal(Path(tmp),job)
            journal.append('request',{'text':'я'*70000});journal.append('result',{'done':True})
            delivered=[]
            def flaky(path,row):
                if row['sequence']==1:raise OSError('temporary transport failure')
                delivered.append(row)
            claim={'command_id':str(uuid.uuid4()),'lease':str(uuid.uuid4())}
            with self.assertRaises(OSError):journal.deliver(SimpleNamespace(transport=flaky),claim)
            Journal(Path(tmp),job).deliver(SimpleNamespace(transport=lambda path,row:delivered.append(row)),claim)
            self.assertEqual([r['sequence'] for r in delivered],list(range(len(delivered))))
            for row in delivered:BatchLogChunk.objects.create(batch=self.batch,**{k:row[k] for k in ('sequence','entries','digest')})
            self.assertEqual(list(stream_events(self.batch.log_chunks))[0]['value']['text'],'я'*70000)

    def test_retention_keeps_entire_recent_stream_and_paused(self):
        from django.core.management import call_command
        from django.utils import timezone
        from datetime import timedelta
        old=timezone.now()-timedelta(days=40)
        self.batch.status='completed';self.batch.save()
        c=BatchLogChunk.objects.create(batch=self.batch,**self.chunk(dict(id='old',kind='result',at=0,value={})))
        BatchLogChunk.objects.filter(pk=c.pk).update(created=old)
        BatchLogChunk.objects.create(batch=self.batch,**self.chunk(dict(id='new',kind='result',at=1,value={}),1))
        call_command('purge_check_logs',verbosity=0)
        self.assertEqual(self.batch.log_chunks.count(),2)
        self.batch.status='paused';self.batch.save()
        self.batch.log_chunks.update(created=old);call_command('purge_check_logs',verbosity=0)
        self.assertEqual(self.batch.log_chunks.count(),2)
        self.batch.status='completed';self.batch.save();call_command('purge_check_logs',verbosity=0)
        self.assertEqual(self.batch.log_chunks.count(),0)
