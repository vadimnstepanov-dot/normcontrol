import copy,json
from django.test import TestCase
from django.contrib.auth.models import User
from .models import Scope,NormativeSet,SourceUpload,AuditEvent
from .source_identity import project
from knowledge_v2.source_identity import FIELDS


class SourceIdentityTests(TestCase):
    def setUp(self):
        self.owner=User.objects.create_user('source-expert');self.other=User.objects.create_user('source-reader-outside')
        self.scope=Scope.objects.create(owner=self.owner,name='ACL',kind='project')
        self.area=NormativeSet.objects.create(scope=self.scope,created_by=self.owner,name='Norms')
        self.source=SourceUpload.objects.create(normative_set=self.area,actor=self.owner,filename='original.docx',sha256='a'*64,size=10,storage_key='test')
        self.client.force_login(self.owner)
    def post(self,revision,**fields):
        return self.client.post(f'/normcontol/api/v2/normative-sets/{self.area.pk}/sources/{self.source.pk}/identity/',json.dumps(dict(expected_revision=revision,fields=fields,reason='Сверено по титульному листу')),content_type='application/json')
    def test_edit_display_history_file_unchanged_and_reanalysis_preserves(self):
        self.assertEqual(self.post(1,short_title='Порядок проектирования',full_title='Полное название',approval_date='24.08.2026',approval_document_number='100/р').status_code,200)
        self.source.refresh_from_db();self.assertEqual(self.source.display_name,'Порядок проектирования · утв. 24.08.2026')
        auto=dict(source_sha256='a'*64,fields={k:dict(value='Новое машинное значение',citations=[]) for k in FIELDS},confidence=.8,status='identified')
        project(self.source,auto,self.owner);self.source.refresh_from_db()
        self.assertEqual(self.source.identification['fields']['short_title']['value'],'Порядок проектирования')
        self.assertEqual(self.source.filename,'original.docx');self.assertEqual(self.source.sha256,'a'*64)
        self.assertEqual(AuditEvent.objects.filter(action='source.identity.edited').count(),1)
    def test_acl_stale_and_only_metadata_fields(self):
        self.client.force_login(self.other);self.assertEqual(self.post(1,short_title='Text').status_code,403)
        self.client.force_login(self.owner);self.assertEqual(self.post(999,short_title='Text').status_code,409)
        self.assertEqual(self.post(1,sha256='b'*64).status_code,400)
