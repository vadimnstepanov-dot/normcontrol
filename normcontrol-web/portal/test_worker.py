import os
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Batch,WorkerRun,ReviewFeedback,WorkerPresence,LLMRuntime

class WorkerTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('a');self.other=User.objects.create_user('b');self.batch=Batch.objects.create(owner=self.user,name='Комплект',status='waiting');self.token='test-worker-token-'+('x'*40);self.env=patch.dict(os.environ,{'NORMCONTROL_WORKER_TOKEN':self.token});self.env.start();self.addCleanup(self.env.stop)
    def test_auth_and_claim_once(self):
        self.assertEqual(self.client.post('/normcontol/worker/claim/',{}).status_code,403)
        r=self.client.post('/normcontol/worker/claim/',{'worker':'test'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token);self.assertEqual(r.status_code,200);self.assertEqual(r.json()['job'],str(self.batch.pk))
        self.assertIsNone(self.client.post('/normcontol/worker/claim/',{},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token).json()['job'])
    def test_ping_publishes_sanitized_rag_details(self):
        rag={'catalog':'catalog-1','requirements':850,'contract_version':1,'unresolved_dependencies':9,
             'settings':{'requirements_per_group':10,'evidence_chars_per_group':52000,'reference_group_size':3,'verification_group_size':3,'search_normalization':'Русская морфология'},
             'sources':[{'name':'СТО РЖД 04.001.1–2021','sha256':'a'*12,'blocks':100,'tables':4,'warnings':2}]}
        response=self.client.post('/normcontol/worker/ping/',{'worker':'desktop','state':'idle','rag':rag},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(response.status_code,200);saved=WorkerPresence.objects.get(name='desktop').details['rag']
        self.assertEqual(saved['requirements'],850);self.assertEqual(saved['sources'][0]['name'],'СТО РЖД 04.001.1–2021')
    def test_llm_telemetry_and_admin_control(self):
        telemetry='/normcontol/worker/llm/telemetry/'
        sample={'online':True,'vram_used_mb':12000,'vram_total_mb':16000,'gpu_percent':73,
                'generation_tps':63.2,'prefill_tps':980.5,'profile':'vision'}
        self.assertEqual(self.client.post(telemetry,{'sample':sample},content_type='application/json').status_code,403)
        for _ in range(125):
            response=self.client.post(telemetry,{'sample':sample},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(response.status_code,200)
        runtime=LLMRuntime.objects.get(pk=1)
        self.assertEqual(len(runtime.history),1)  # duplicate polls in one minute are coalesced
        runtime.history=[{'at':(timezone.now()-timedelta(seconds=30*i)).isoformat()} for i in reversed(range(130))]
        runtime.save(update_fields=['history'])
        self.client.post(telemetry,{'sample':sample},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(len(LLMRuntime.objects.get(pk=1).history),120)
        self.assertNotIn('model_path',runtime.sample)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/normcontol/llm/status/').json()['sample']['generation_tps'],63.2)
        self.assertEqual(self.client.post('/normcontol/llm/action/',{'action':'restart'},content_type='application/json').status_code,403)
        self.user.is_staff=True;self.user.save(update_fields=['is_staff'])
        response=self.client.post('/normcontol/llm/action/',{'action':'restart'},content_type='application/json')
        self.assertEqual(response.status_code,200)
        command=response.json()['command']
        self.assertEqual(self.client.get('/normcontol/worker/llm/command/',HTTP_AUTHORIZATION='Bearer '+self.token).json()['command']['action'],'restart')
        self.assertEqual(self.client.post('/normcontol/llm/action/',{'action':'stop'},content_type='application/json').status_code,409)
        self.client.post(telemetry,{'sample':sample,'ack':command['id'],'success':True},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(LLMRuntime.objects.get(pk=1).command['state'],'done')
    def test_claim_respects_admin_queue_order(self):
        priority=Batch.objects.create(owner=self.other,name='Приоритетный',status='waiting',queue_position=-1)
        result=self.client.post('/normcontol/worker/claim/',{'worker':'ordered'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(result.json()['job'],str(priority.pk))
    def test_result_idempotence_and_isolation(self):
        run=WorkerRun.objects.create(batch=self.batch,worker='x');url=f'/normcontol/worker/{run.lease}/update/'
        for seq,state in [(2,'running'),(1,'failed')]:self.client.post(url,{'sequence':seq,'state':state,'snapshot':{'id':'test'}},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        run.refresh_from_db();self.assertEqual(run.state,'running');self.assertEqual(run.sequence,2)
        self.client.force_login(self.other);self.assertEqual(self.client.get(f'/normcontol/batches/{self.batch.pk}/status/').status_code,404);self.assertEqual(self.client.get(f'/normcontol/batches/{self.batch.pk}/report/').status_code,404)
        self.client.force_login(self.user);self.assertEqual(self.client.get(f'/normcontol/batches/{self.batch.pk}/status/').status_code,200)
    def test_arbitrary_path_is_not_a_file_endpoint(self):
        self.assertEqual(self.client.get('/normcontol/worker/files/?path=C:/secret',HTTP_AUTHORIZATION='Bearer '+self.token).status_code,404)
    def test_polling_keeps_large_report_out_of_status_and_updates(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        run=WorkerRun.objects.create(batch=self.batch,worker='desktop',local_id='job',report={'findings':[],'large':'x'*200000})
        self.client.force_login(self.user)
        with CaptureQueriesContext(connection) as queries:
            response=self.client.get(f'/normcontol/batches/{self.batch.pk}/status/')
        self.assertNotIn('report',response.json());self.assertTrue(response.json()['report_available'])
        self.assertLess(len(response.content),2000)
        self.assertFalse(any('"portal_workerrun"."report"' in q['sql'] for q in queries))
        with CaptureQueriesContext(connection) as queries:
            self.client.post(f'/normcontol/worker/{run.lease}/update/',{'sequence':1,'state':'running','local_id':'job','snapshot':{}},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertFalse(any('"report" =' in q['sql'] or '"portal_workerrun"."report"' in q['sql'] for q in queries))
        run.refresh_from_db();self.assertEqual(len(run.report['large']),200000)
        page=self.client.get(f'/normcontol/batches/{self.batch.pk}/');self.assertContains(page,'Ход нормоконтроля')
        title=page.content.decode().split('<title>')[1].split('</title>')[0]
        self.assertNotIn('<form',title)
    def test_status_can_include_bounded_findings_preview(self):
        WorkerRun.objects.create(batch=self.batch,worker='desktop',state='running',report={'findings':[{'id':str(i)} for i in range(80)],'tasks':[{'id':'old-failure','stage':'sto','state':'failed','error':'Старый сбой'}]})
        self.client.force_login(self.user)
        response=self.client.get(f'/normcontol/batches/{self.batch.pk}/status/?include_findings=1')
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['findings_preview']),60)
        self.assertEqual(response.json()['snapshot']['task_errors'][0]['error'],'Старый сбой')
    def test_frontend_wake_is_relayed_to_active_desktop_worker(self):
        run=WorkerRun.objects.create(batch=self.batch,worker='desktop',state='running',snapshot={})
        self.client.force_login(self.user)
        response=self.client.post(f'/normcontol/batches/{self.batch.pk}/wake/')
        self.assertEqual(response.status_code,200);self.assertTrue(response.json()['signalled'])
        update=self.client.post(f'/normcontol/worker/{run.lease}/update/',{'sequence':1,'state':'running','snapshot':{}},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertTrue(update.json()['wake'])
        run.refresh_from_db();self.assertIn('_frontend_wake_at',run.snapshot)
    def test_claim_response_can_be_replayed_without_duplicate(self):
        values=[self.client.post('/normcontol/worker/claim/',{'worker':'same'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token).json() for _ in range(2)]
        self.assertEqual(values[0]['lease'],values[1]['lease']);self.assertEqual(WorkerRun.objects.count(),1)
    def test_large_report_pagination_filters_and_complete_export(self):
        findings=[{'id':str(i),'status':'confirmed','issue':'Замечание-'+str(i).zfill(3),
            'category':'грамотность' if i<50 else 'техническая логика','severity':'minor' if i<50 else 'major',
            'explanation':'Проверка','suggestion':'Уточнить',
            'evidence':[{'document':'a' if i<50 else 'b','address':'пункт 4.2','quote':'Текст'}]} for i in range(51)]
        WorkerRun.objects.create(batch=self.batch,worker='desktop',report={'findings':findings,
            'documents':[{'id':'a','name':'ТЗ.docx'},{'id':'b','name':'ОИТ.docx'}],
            'tasks':[{'id':'failed-1','stage':'sto','state':'failed','error':'Тайм-аут модели'}]})
        self.client.force_login(self.user);url=f'/normcontol/batches/{self.batch.pk}/report/'
        first=self.client.get(url);self.assertContains(first,'Замечание-049');self.assertNotContains(first,'Замечание-050')
        second=self.client.get(url,{'page':2});self.assertContains(second,'Замечание-050');self.assertNotContains(second,'Замечание-049')
        filtered=self.client.get(url,{'document':'b','category':'техническая логика','severity':'major','q':'4.2'})
        self.assertContains(filtered,'Замечание-050');self.assertEqual(filtered.context['finding_page'].paginator.count,1)
        self.assertEqual(len(self.client.get(url,{'format':'json'}).json()['findings']),51)
        register=f'/normcontol/batches/{self.batch.pk}/register/'
        first_page=self.client.get(register,{'status':'all'}).json()
        self.assertEqual(first_page['total'],52);self.assertEqual(len(first_page['records']),50)
        self.assertEqual(len(self.client.get(register,{'status':'all','page':2}).json()['records']),2)
        self.assertIn('grammar',[x['value'] for x in first_page['types']])
        self.assertEqual(self.client.get(register,{'status':'all','type':'grammar'}).json()['total'],50)
        self.assertEqual(self.client.get(register,{'status':'task-error'}).json()['records'][0]['value']['error'],'Тайм-аут модели')
        excel=self.client.get(url,{'status':'all','format':'xlsx'})
        word=self.client.get(url,{'status':'all','format':'docx'})
        self.assertEqual(excel.status_code,200);self.assertEqual(word.status_code,200)
        with zipfile.ZipFile(io.BytesIO(excel.content)) as workbook:
            self.assertIsNone(workbook.testzip())
            for name in workbook.namelist():
                if name.endswith('.xml'):ET.fromstring(workbook.read(name))
            sheet=workbook.read('xl/worksheets/sheet2.xml').decode()
            self.assertIn('Замечание-050',sheet);self.assertIn('Замечание-000',sheet)
            self.assertEqual(sheet.count('<row '),52)
            self.assertIn('Тайм-аут модели',workbook.read('xl/worksheets/sheet3.xml').decode())
        with zipfile.ZipFile(io.BytesIO(word.content)) as document:
            self.assertIsNone(document.testzip())
            for name in document.namelist():
                if name.endswith('.xml'):ET.fromstring(document.read(name))
            body=document.read('word/document.xml').decode()
            self.assertIn('Замечание-050',body);self.assertIn('Замечание-000',body);self.assertIn('Тайм-аут модели',body)
            self.assertIn('Обоснование: Проверка',body);self.assertIn('Цитата: Текст',body);self.assertIn('Предложение: Уточнить',body)
        filtered_excel=self.client.get(url,{'status':'all','type':'grammar','format':'xlsx'})
        with zipfile.ZipFile(io.BytesIO(filtered_excel.content)) as workbook:
            sheet=workbook.read('xl/worksheets/sheet2.xml').decode()
            self.assertIn('Замечание-049',sheet);self.assertNotIn('Замечание-050',sheet)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url,{'status':'all','format':'xlsx'}).status_code,404)
        self.assertEqual(self.client.get(register).status_code,404)
    def test_feedback_after_completion_and_pause_control(self):
        run=WorkerRun.objects.create(batch=self.batch,worker='desktop',state='partial',local_id='local-job')
        ReviewFeedback.objects.create(batch=self.batch,author=self.user,finding_id='f',comment='Проверьте исходную формулировку.')
        result=self.client.post('/normcontol/worker/feedback/',{'worker':'desktop'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        self.assertEqual(result.json()['feedback'][0]['local_id'],'local-job')
        self.assertEqual(self.client.post('/normcontol/worker/feedback/',{'worker':'different'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token).json()['feedback'],[])
        run.control='pause';run.save()
        self.client.post(f'/normcontol/worker/{run.lease}/update/',{'sequence':1,'state':'paused'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+self.token)
        run.refresh_from_db();self.batch.refresh_from_db();self.assertEqual(run.control,'');self.assertEqual(self.batch.status,'paused')
