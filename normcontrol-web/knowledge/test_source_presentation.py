import json,time,uuid
from urllib.parse import urlsplit
from unittest.mock import patch
from django.test import SimpleTestCase,Client
from .source_names import names
from .test_uploads import UploadTests
from knowledge_v2.tests.test_ingest import docx
from pathlib import Path

class SourceNameTests(SimpleTestCase):
    def metadata(self,**fields):return {'fields':{k:{'value':v} for k,v in fields.items()}}
    def test_requested_standard_names(self):
        title='Порядок согласования и утверждения документов, разрабатываемых при создании и модификации автоматизированных систем и программных средств'
        identity=self.metadata(short_title='СТО РЖД 04.001.4–2021 · '+title+' (1755/р от 24.08.2026)',full_title='Автоматизированные системы и программные средства ОАО «РЖД». '+title,approval_document_number='1755/р',approval_document_date='2026-08-24',approval_date='2026-08-24')
        short,full=names(identity,'original.docx')
        self.assertEqual(short,'СТО РЖД 04.001.4 · '+title.replace('автоматизированных систем и программных средств','АС (ПС)'))
        self.assertEqual(full,'СТО РЖД 04.001.4–2021 · '+title+' (1755/р от 24.08.2026) · утв. 2026-08-24')
        # Formatting is idempotent even after expert saves the displayed requisites.
        identity['fields']['short_title']['value']=short;identity['fields']['full_title']['value']=full
        self.assertEqual(names(identity,'original.docx'),(short,full))
    def test_generic_missing_and_nonstandard_titles(self):
        self.assertEqual(names({},'original.docx'),('original.docx','original.docx'))
        short,full=names(self.metadata(short_title='План внедрения (1755/р от 24.08.2026)',full_title='План внедрения',approval_document_number='1755/р',approval_document_date='24.08.2026'),'f.doc')
        self.assertEqual(short,'План внедрения');self.assertEqual(full,'План внедрения (1755/р от 24.08.2026)')
        short,full=names(self.metadata(short_title='СТО РЖД 04.001.0–2021 · Общие положения',full_title='Автоматизированные системы и программные средства ОАО «РЖД». Общие положения'),'f.docx')
        self.assertEqual(short,'СТО РЖД 04.001.0 · Общие положения');self.assertEqual(full,'СТО РЖД 04.001.0–2021 · Общие положения')

class SourceWordTests(UploadTests):
    def ready_word(self):
        self.original=docx(Path(self.tmp.name)/'source.docx').read_bytes()
        r=self.upload(self.original);self.sid=r.json()['sources'][0]['id']
        self.base=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{self.sid}/'
    def issued(self):
        r=self.client.post(self.base+'open/',data='{}',content_type='application/json');self.assertEqual(r.status_code,200,r.content)
        self.assertIn('no-store',r['Cache-Control']);self.assertTrue(r.json()['word_url'].startswith('ms-word:ofv|u|'))
        url=r.json()['word_url'].removeprefix('ms-word:ofv|u|');p=urlsplit(url);return p.path+'?'+p.query
    def test_word_reads_exact_original_without_browser_cookie(self):
        self.ready_word();url=self.issued();r=Client().get(url)
        self.assertEqual(r.status_code,200);self.assertEqual(b''.join(r.streaming_content),self.original)
        self.assertTrue(r['Content-Disposition'].startswith('inline'));self.assertIn('no-store',r['Cache-Control']);r.close()
        r=Client().head(url);self.assertEqual(r.status_code,200);self.assertEqual(int(r['Content-Length']),len(self.original));r.close()
        self.assertEqual(Client().get(self.base+'word/').status_code,403)
        self.assertEqual(Client().post(url).status_code,405)
    def test_expiry_tamper_source_binding_and_revoked_user(self):
        self.ready_word();url=self.issued()
        with patch('django.core.signing.time.time',return_value=time.time()+360):self.assertEqual(Client().get(url).status_code,403)
        self.assertEqual(Client().get(url.replace(self.sid,str(uuid.uuid4()))).status_code,403)
        self.assertEqual(Client().get(url+'x').status_code,403)
        self.user.is_active=False;self.user.save();self.assertEqual(Client().get(url).status_code,403)
    def test_outside_user_cannot_issue_word_link(self):
        self.ready_word();self.client.force_login(self.other)
        self.assertEqual(self.client.post(self.base+'open/',data='{}',content_type='application/json').status_code,403)
