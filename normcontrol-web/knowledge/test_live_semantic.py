"""Opt-in isolated portal integration against an explicitly configured local LLM."""
import io
import json
import os
import unittest
from pathlib import Path
from zipfile import ZipFile
from django.test import TestCase,override_settings
from .test_uploads import UploadTests,TOKEN
from .models import DocumentProfile


@unittest.skipUnless(os.getenv('KNOWLEDGE_LIVE_SEMANTIC_TEST')=='1','Opt-in local LLM test')
@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class LiveSemanticPortalTest(TestCase):
    setUp=UploadTests.setUp
    transport=UploadTests.transport
    download=UploadTests.download
    upload=UploadTests.upload

    def test_api_to_real_llm_to_canonical_and_profiles(self):
        from knowledge_v2.structural_model import StructuralClient
        from knowledge_v2.store import KnowledgeStore
        self.bridge.analysis_client=StructuralClient(os.environ['KNOWLEDGE_LLM_ENDPOINT'],'local-qwen',KnowledgeStore('/queue'),
            api_key=os.environ.get('KNOWLEDGE_LLM_API_KEY',''),revision='Qwen3.8-27B-UD-Q4_K_S-20260926',
            max_tokens=6000,measure_context=True)
        content=io.BytesIO()
        with ZipFile(content,'w') as archive:
            archive.writestr('word/document.xml','''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Общие требования</w:t></w:r></w:p>
            <w:p><w:r><w:t>Документ должен содержать дату утверждения.</w:t></w:r></w:p>
            <w:p><w:r><w:t>Версия — состояние документа на определённую дату.</w:t></w:r></w:p>
            </w:body></w:document>''')
        sid=self.upload(content.getvalue()).json()['sources'][0]['id'];self.bridge.once()
        url=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/sources/{sid}/analysis/'
        response=self.client.post(url,data='{}',content_type='application/json',HTTP_IDEMPOTENCY_KEY='real-model-control')
        self.assertEqual(response.status_code,202)
        self.bridge.once()
        result=self.client.get(url).json()
        self.assertEqual(result['state'],'done');self.assertTrue(result['entries'])
        self.assertTrue(any(e.get('entity_type')=='definition' for e in result['entries']))
        self.assertTrue(any(e.get('entity_type')=='requirement' for e in result['entries']))
        self.assertTrue(all(e['expert_status']=='unreviewed' for e in result['entries']))
        self.assertGreater(DocumentProfile.objects.filter(scope=self.scope).count(),0)
        with self.store.connection() as db:
            kinds={r[0]:r[1] for r in db.execute('SELECT kind,count(*) FROM records GROUP BY kind')}
        self.assertGreater(kinds.get('term_definition',0),0);self.assertGreater(kinds.get('requirement',0),0)
        out=os.getenv('KNOWLEDGE_LIVE_TEST_REPORT')
        if out:Path(out).write_text(json.dumps(dict(api=result,canonical_counts=kinds,profiles=DocumentProfile.objects.count()),
            ensure_ascii=False,indent=2),encoding='utf8')
