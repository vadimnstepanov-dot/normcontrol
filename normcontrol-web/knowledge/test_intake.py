"""User journeys for one upload + one launch confirmation (isolated storage)."""
import tempfile,uuid
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied
from portal.models import Batch,Document,LaunchReceipt,WorkerRun
from portal.tests import document
from .models import Scope,NormativeSet,Release,KnowledgeCheck,Command
from .services import NotReady

@override_settings(KNOWLEDGE_V2_ENABLED=True)
class SingleScreenLaunchTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('one-screen-owner');self.client.force_login(self.user)
        self.temp=tempfile.TemporaryDirectory();self.setting=override_settings(MEDIA_ROOT=self.temp.name);self.setting.enable()
        self.addCleanup(self.setting.disable);self.addCleanup(self.temp.cleanup)
        self.scope=Scope.objects.create(owner=self.user,name='Scope',kind='project')
        self.norm=self.make_set(self.scope,'Norms');self.key=str(uuid.uuid4())
    def make_set(self,scope,name,purpose='normative'):
        item=NormativeSet.objects.create(scope=scope,name=name,created_by=self.user,state='ready',purpose=purpose)
        release=Release.objects.create(id=uuid.uuid4(),normative_set=item,state='active',manifest_hash='a'*64,attestation={},
            manifest={'generation_id':'fixture','embedding_space':'fixture','versions':{'curation_digest':'test'},'items':[]})
        item.active_release=release;item.save();return item
    def submit(self,**changes):
        data={'checks':['sto','logic','language'],'normative_sets':[str(self.norm.pk)],'launch_key':self.key,'documents':[document('Example_ОИТ.docx')]}
        data.update(changes)
        return self.client.post('/normcontol/batches/new/',data,HTTP_X_REQUESTED_WITH='XMLHttpRequest')
    def test_one_screen_has_files_checks_norms_and_exactly_one_launch(self):
        page=self.client.get('/normcontol/batches/new/')
        for text in ('name="documents"','name="checks"','name="normative_sets"','name="launch_key"','Запустить проверку'):
            self.assertContains(page,text)
        self.assertContains(page,'id="launch-submit"',count=1)
        self.assertNotContains(page,'На следующем шаге');self.assertNotContains(page,'Проверить файлы')
        self.assertEqual(page.context['form'].initial['normative_sets'],[str(self.norm.pk)])
    def test_submit_queues_native_and_sto_atomically_and_opens_progress(self):
        result=self.submit();self.assertEqual(result.status_code,200)
        batch=Batch.objects.get();self.assertEqual(batch.status,'waiting');self.assertEqual(batch.name,'Example_ОИТ')
        self.assertEqual(batch.checks,['sto','logic','language']);self.assertEqual(batch.documents.count(),1)
        self.assertEqual(result.json()['next'],f'/normcontol/?batch={batch.pk}#current-review')
        self.assertEqual(KnowledgeCheck.objects.count(),1);self.assertEqual(Command.objects.count(),1)
        self.assertTrue(Command.objects.get().payload['after_non_normative']);self.assertEqual(LaunchReceipt.objects.count(),1)
    def test_duplicate_post_never_creates_second_batch_snapshot_or_file(self):
        first=self.submit();second=self.submit()
        self.assertEqual(first.json()['next'],second.json()['next'])
        self.assertEqual(Batch.objects.count(),1);self.assertEqual(Document.objects.count(),1)
        self.assertEqual(KnowledgeCheck.objects.count(),1);self.assertEqual(Command.objects.count(),1)
        self.assertEqual(len(list(Path(self.temp.name).rglob('*.docx'))),1)
        changed=self.submit(checks=['sto']);self.assertEqual(changed.status_code,422)
        self.assertEqual(Batch.objects.count(),1)
    def test_missing_normative_selection_stays_on_same_form_without_storage(self):
        response=self.submit(normative_sets=[]);self.assertEqual(response.status_code,422)
        self.assertIn('normative_sets',response.json()['errors']);self.assertFalse(Batch.objects.exists())
        self.assertFalse(list(Path(self.temp.name).rglob('*.docx')))
    def test_bad_second_file_cannot_queue_partial_package(self):
        response=self.submit(documents=[document(),SimpleUploadedFile('broken.docx',b'bad')])
        self.assertEqual(response.status_code,422);self.assertFalse(Batch.objects.exists());self.assertFalse(Command.objects.exists())
    def test_release_failure_after_saving_files_rolls_back_and_removes_files(self):
        with patch('knowledge.launch.start',side_effect=NotReady('Release changed')):
            response=self.submit()
        self.assertEqual(response.status_code,422);self.assertFalse(Batch.objects.exists());self.assertFalse(LaunchReceipt.objects.exists())
        self.assertFalse(list(Path(self.temp.name).rglob('*.docx')))
    def test_acl_change_after_form_validation_is_readable_and_leaves_no_package(self):
        with patch('knowledge.launch.start',side_effect=PermissionDenied('Access changed')):
            response=self.submit()
        self.assertEqual(response.status_code,422);self.assertIn('Доступ к нормативной базе изменился',response.json()['errors']['__all__'][0]['message'])
        self.assertFalse(Batch.objects.exists());self.assertFalse(list(Path(self.temp.name).rglob('*.docx')))
    def test_mixed_scope_allowed_and_wrong_experience_rejected_before_upload(self):
        other_scope=Scope.objects.create(owner=self.user,name='Other scope',kind='project')
        other=self.make_set(other_scope,'Other norms');experience=self.make_set(other_scope,'Experience','experience')
        response=self.submit(experience=str(experience.pk))
        self.assertEqual(response.status_code,422);self.assertIn('experience',response.json()['errors']);self.assertFalse(Batch.objects.exists())
        response=self.submit(normative_sets=[str(self.norm.pk),str(other.pk)])
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(KnowledgeCheck.objects.get().snapshot.data['releases']),2)
    def test_explicit_no_sto_does_not_create_normative_command(self):
        result=self.submit(checks=['logic','language'],normative_sets=[],experience='')
        self.assertEqual(result.status_code,200);self.assertEqual(Batch.objects.get().checks,['logic','language'])
        self.assertFalse(KnowledgeCheck.objects.exists());self.assertFalse(Command.objects.exists())
    def test_sto_only_never_creates_native_run_or_waiting_package(self):
        self.assertEqual(self.submit(checks=['sto']).status_code,200)
        self.assertEqual(Batch.objects.get().status,'running');self.assertFalse(WorkerRun.objects.exists())
    def test_private_norms_unavailable_and_public_form_has_csrf_protection(self):
        other=User.objects.create_user('other-owner');self.client.force_login(other)
        self.assertEqual(self.submit().status_code,422);self.assertFalse(Batch.objects.exists())
        from django.test import Client
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        self.assertEqual(client.post('/normcontol/batches/new/',{}).status_code,403)
    def test_no_directions_files_or_launch_key_cannot_create_package(self):
        for changes in ({'checks':[]},{'documents':[]},{'launch_key':''}):
            self.assertEqual(self.submit(**changes).status_code,422)
        self.assertFalse(Batch.objects.exists())
    def test_worker_offline_shows_queue_behavior_without_requiring_second_confirmation(self):
        page=self.client.get('/normcontol/batches/new/')
        self.assertContains(page,'пакет сохранится в очереди');self.assertContains(page,'Повторное подтверждение не потребуется')
