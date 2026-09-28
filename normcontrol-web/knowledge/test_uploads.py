import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from . import services as service
from .models import SourceUpload, Command

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'sto_rag'))
from knowledge_v2.store import KnowledgeStore
from knowledge_v2.bridge import Bridge
from knowledge_v2.tests.test_ingest import docx

TOKEN='test-only-v2-token-not-a-real-secret-12345'


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class UploadTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.over=override_settings(DATA_DIR=Path(self.tmp.name)/'portal');self.over.enable();self.addCleanup(self.over.disable)
        self.user=get_user_model().objects.create_user('uploader-stage3')
        self.other=get_user_model().objects.create_user('stranger-stage3')
        self.scope=service.create_scope(self.user,'Private','personal')
        self.dataset=service.create_set(self.user,'New standards',self.scope.pk,'set-key')
        self.client.force_login(self.user)
        self.store=KnowledgeStore(Path(self.tmp.name)/'canonical')
        self.bridge=Bridge(self.store,'http://localhost/normcontol/api/v2',TOKEN,self.transport,self.download)
        self.assertTrue(self.bridge.once())  # register empty set first

    def transport(self,path,payload):
        r=Client().post('/normcontol/api/v2'+path,data=json.dumps(payload),content_type='application/json',
                        HTTP_AUTHORIZATION='Bearer '+TOKEN)
        if r.status_code!=200:raise RuntimeError((r.status_code,r.json()))
        return r.json()

    def download(self,command_id,source_id,lease):
        r=Client().get(f'/normcontol/api/v2/worker/commands/{command_id}/sources/{source_id}/',
                       HTTP_AUTHORIZATION='Bearer '+TOKEN,HTTP_X_KNOWLEDGE_LEASE=lease)
        if r.status_code!=200:raise RuntimeError((r.status_code,r.json()))
        try:return contextlib.closing(io.BytesIO(b''.join(r.streaming_content)))
        finally:r.close()

    def upload(self,content,name='rule.docx'):
        return self.client.post(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/upload/',
            {'files':[SimpleUploadedFile(name,content,content_type='application/octet-stream')]})

    def test_upload_deduplicate_worker_parse_and_original(self):
        source=docx(Path(self.tmp.name)/'source.docx',image=True).read_bytes()
        response=self.upload(source);self.assertEqual(response.status_code,201,response.content)
        source_id=response.json()['sources'][0]['id']
        self.assertEqual(self.upload(source).json()['sources'][0]['duplicate'],True)
        self.assertEqual(SourceUpload.objects.count(),1)
        self.assertTrue(self.bridge.once())
        row=SourceUpload.objects.get(pk=source_id)
        self.assertEqual(row.state,'partial')
        self.assertEqual(row.result['fragment_count'],4)
        self.assertEqual(row.result['coverage_summary']['unreadable'],1)
        coverage=self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{source_id}/coverage/')
        self.assertEqual(coverage.status_code,200)
        self.assertEqual(coverage.json()['total'],len(coverage.json()['entries']))
        self.assertTrue(any(x['state']=='unreadable' for x in coverage.json()['entries']))
        self.assertEqual(self.store.counts()['records'],5)
        response=self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{source_id}/download/')
        self.assertEqual(b''.join(response.streaming_content),source);response.close()
        self.assertFalse(self.bridge.once())

    def test_rejects_bad_member_without_partial_registration(self):
        valid=docx(Path(self.tmp.name)/'valid.docx').read_bytes()
        r=self.client.post(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/upload/',
            {'files':[SimpleUploadedFile('good.docx',valid),SimpleUploadedFile('bad.pdf',b'garbage')]})
        self.assertEqual(r.status_code,400)
        self.assertEqual(SourceUpload.objects.count(),0)
        self.assertFalse(Command.objects.filter(kind='source.ingest').exists())

    def test_access_and_worker_lease(self):
        source=docx(Path(self.tmp.name)/'source.docx').read_bytes()
        source_id=self.upload(source).json()['sources'][0]['id']
        url=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{source_id}/'
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.get(url+'coverage/').status_code,403)
        self.assertEqual(self.client.get(url+'download/').status_code,403)
        c=Command.objects.get(kind='source.ingest');worker_path=f'/normcontol/api/v2/worker/commands/{c.pk}/sources/{source_id}/'
        self.assertEqual(Client().get(worker_path,HTTP_AUTHORIZATION='Bearer '+TOKEN).status_code,403)
        claim=service.claim('test-worker',['source.ingest'])
        self.assertEqual(Client().get(worker_path,HTTP_AUTHORIZATION='Bearer '+TOKEN,HTTP_X_KNOWLEDGE_LEASE='bad').status_code,403)
        response=Client().get(worker_path,HTTP_AUTHORIZATION='Bearer '+TOKEN,HTTP_X_KNOWLEDGE_LEASE=claim['lease'])
        self.assertEqual(response.status_code,200);response.close()

    def test_new_revision_invalidates_changed_section_and_same_upload_is_free(self):
        first=docx(Path(self.tmp.name)/'first.docx').read_bytes()
        source_id=self.upload(first).json()['sources'][0]['id'];self.bridge.once()
        second=docx(Path(self.tmp.name)/'second.docx',paragraph='Новое условие.').read_bytes()
        response=self.client.post(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/upload/',
            {'files':[SimpleUploadedFile('second.docx',second)],'supersedes':source_id})
        self.assertEqual(response.status_code,201,response.content)
        self.bridge.once()
        new=SourceUpload.objects.get(pk=response.json()['sources'][0]['id'])
        self.assertEqual(str(new.supersedes_id),source_id)
        self.assertGreater(new.result['reuse']['invalidated_contexts'],0)
        self.assertEqual(self.store.counts()['records'],10)

    def test_incomplete_coverage_cannot_confirm_source(self):
        source=docx(Path(self.tmp.name)/'source.docx').read_bytes()
        source_id=self.upload(source).json()['sources'][0]['id']
        claim=service.claim('test-worker',['source.ingest'])
        payload=dict(kind='source.ingested',set_id=str(self.dataset.pk),source_id=source_id,
                     sha256=SourceUpload.objects.get(pk=source_id).sha256,parser_version='structure-v2.2',
                     fragment_count=1,classification={},coverage_summary={},coverage_count=1,
                     coverage_digest='0'*64,issues=[],counts={},reuse={})
        with self.assertRaises(service.NotReady):service.accept_event('test-worker',__import__('uuid').uuid4(),
            claim['command_id'],claim['lease'],payload,service.digest(payload))
        self.assertEqual(SourceUpload.objects.get(pk=source_id).state,'processing')

    def test_large_coverage_is_paged_without_losing_entries(self):
        path=Path(self.tmp.name)/'large.docx'
        para='<w:p><w:r><w:t>Требование</w:t></w:r></w:p>'
        xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+para*1100+'</w:body></w:document>'
        with ZipFile(path,'w') as archive:archive.writestr('word/document.xml',xml)
        sid=self.upload(path.read_bytes()).json()['sources'][0]['id']
        self.bridge.once()
        base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{sid}/coverage/'
        pages=[self.client.get(base,{'page':i}).json() for i in range(3)]
        self.assertEqual(sum(len(p['entries']) for p in pages),1101)  # includes unknown type
        self.assertEqual([len(p['entries']) for p in pages],[500,500,101])
        self.assertEqual(SourceUpload.objects.get(pk=sid).result['coverage_count'],1101)

    def test_permanent_processing_error_is_visible_without_waiting_for_lease(self):
        source=docx(Path(self.tmp.name)/'source.docx').read_bytes()
        sid=self.upload(source).json()['sources'][0]['id']
        with patch.object(self.bridge,'downloader',side_effect=ValueError('bad source')):
            with self.assertRaises(ValueError):self.bridge.once()
        row=SourceUpload.objects.get(pk=sid)
        self.assertEqual(row.state,'error')
        self.assertEqual(row.result['reason'],'invalid_source')
        self.assertEqual(Command.objects.get(kind='source.ingest').state,'failed')
        response=self.client.post(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{sid}/retry/',
                                  data='{}',content_type='application/json')
        self.assertEqual(response.status_code,202)
        self.assertTrue(self.bridge.once())
        self.assertEqual(SourceUpload.objects.get(pk=sid).state,'prepared')
