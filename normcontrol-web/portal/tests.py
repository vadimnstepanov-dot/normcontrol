import io,os,tempfile,zipfile
from unittest.mock import patch
from cryptography.fernet import Fernet
from django.test import TestCase,override_settings,Client
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import Batch,Document,LLMConfig,AccessProfile,LoginAttempt,WorkerRun,WorkerPresence,FindingDisposition

def document(name='test.docx',text='Тестовый документ'):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
    return SimpleUploadedFile(name,out.getvalue())

class PortalTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('owner',password='Testing-Password-For-2026')
        self.other=User.objects.create_user('other',password='Testing-Password-For-2026')
        self.admin=User.objects.create_user('administrator',password='Testing-Password-For-2026',is_staff=True)
        self.temp=tempfile.TemporaryDirectory();self.setting=override_settings(MEDIA_ROOT=self.temp.name);self.setting.enable()
        self.addCleanup(self.setting.disable);self.addCleanup(self.temp.cleanup)
    def test_private_pages_and_csrf(self):
        self.assertEqual(self.client.get('/normcontol/').status_code,302)
        self.assertEqual(self.client.get('/LLM/').status_code,308)
        c=Client(enforce_csrf_checks=True);c.force_login(self.user)
        self.assertEqual(c.post('/normcontol/batches/new/',{}).status_code,403)
    def test_upload_queue_and_ownership(self):
        self.client.force_login(self.user)
        response=self.client.post('/normcontol/batches/new/',{'name':'Проверка','profile':'chtz','checks':['sto','logic'],'documents':[document()]})
        self.assertEqual(response.status_code,302);b=Batch.objects.get();d=b.documents.get()
        self.assertEqual(d.paragraphs,0) # Deep document parsing belongs to the desktop worker.
        self.assertEqual(b.status,'prepared')
        self.assertContains(response=self.client.get(f'/normcontol/batches/{b.pk}/'),text='Подтвердите состав перед запуском')
        self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'queue'})
        b.refresh_from_db();self.assertEqual(b.status,'waiting')
        self.assertContains(self.client.get(f'/normcontol/batches/{b.pk}/'),'test.docx')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/normcontol/batches/{b.pk}/').status_code,404)
        self.assertEqual(self.client.get(f'/normcontol/documents/{d.pk}/download/').status_code,404)
    def test_dashboard_current_review_button_targets_live_section(self):
        self.client.force_login(self.user)
        batch=Batch.objects.create(owner=self.user,name='Текущая',status='running',checks=['sto'])
        WorkerRun.objects.create(batch=batch,worker='desktop',state='running',snapshot={'stages':{'sto':{'done':2,'pending':2}}})
        response=self.client.get('/normcontol/')
        self.assertContains(response,'Текущая проверка');self.assertContains(response,'href="#current-review"');self.assertContains(response,'id="current-review-ring"')
    def test_invalid_upload_is_atomic(self):
        self.client.force_login(self.user)
        response=self.client.post('/normcontol/batches/new/',{'name':'Broken','profile':'chtz','checks':['sto'],'documents':[document(),SimpleUploadedFile('fake.docx',b'not a zip')]})
        self.assertEqual(response.status_code,200);self.assertEqual(Batch.objects.count(),0)
        self.assertContains(response,'не является корректным')
    def test_legacy_doc_upload_is_accepted_and_bad_signature_rejected(self):
        self.client.force_login(self.user)
        legacy=SimpleUploadedFile('legacy.doc',b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'+b'legacy-word-fixture')
        response=self.client.post('/normcontol/batches/new/',{'name':'Legacy','profile':'other','checks':['sto'],'documents':[legacy]})
        self.assertEqual(response.status_code,302);self.assertEqual(Document.objects.get().name,'legacy.doc')
        bad=SimpleUploadedFile('fake.doc',b'not-a-word-document')
        response=self.client.post('/normcontol/batches/new/',{'name':'Bad legacy','profile':'other','checks':['sto'],'documents':[bad]})
        self.assertEqual(response.status_code,200);self.assertContains(response,'двоичным документом Word')
    def test_admin_settings_encrypt_secret_and_make_no_requests(self):
        self.client.force_login(self.user);self.assertEqual(self.client.get('/normcontol/settings/llm/').status_code,302)
        self.client.force_login(self.admin)
        with patch.dict(os.environ,{'APP_CRYPT_KEY':Fernet.generate_key().decode()}):
            response=self.client.post('/normcontol/settings/llm/',{'name':'Local','endpoint':'https://example.com/v1','model':'local-qwen','api_key':'secret-for-test','context_tokens':20480,'output_tokens':6144,'concurrency':1,'timeout_seconds':300})
        self.assertEqual(response.status_code,302)
        config=LLMConfig.objects.get();self.assertNotIn('secret-for-test',config.key_encrypted)
        self.assertNotContains(self.client.get('/normcontol/settings/llm/'),'secret-for-test')
    def test_forced_password_change(self):
        AccessProfile.objects.create(user=self.user,must_change_password=True);self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/normcontol/'),'/normcontol/account/password/')
    def test_all_admin_templates(self):
        self.client.force_login(self.admin)
        for path in ('/normcontol/','/normcontol/batches/new/','/normcontol/reports/','/normcontol/settings/llm/','/normcontol/settings/users/','/normcontol/settings/audit/','/normcontol/account/password/'):
            response=self.client.get(path);self.assertEqual(response.status_code,200,path)
    def test_dashboard_has_live_workspace_controls(self):
        self.client.force_login(self.admin)
        batch=Batch.objects.create(owner=self.admin,name='Живая проверка',checks=['sto'],status='running')
        WorkerRun.objects.create(batch=batch,worker='desktop',state='running',local_id='local',snapshot={'stages':{'sto':{'done':2,'pending':1}},'findings':{'confirmed':1}},report={'findings':[{'id':'f1','status':'confirmed','severity':'major','category':'СТО','issue':'Проверка интерфейса','explanation':'Описание','evidence':[],'suggestion':'Исправить'}]})
        response=self.client.get('/normcontol/',{'batch':batch.pk})
        self.assertContains(response,'ТЕКУЩАЯ ПРОВЕРКА')
        self.assertContains(response,'Выявленные замечания')
        self.assertContains(response,'Проверка интерфейса')

    def test_user_can_set_finding_workflow_state(self):
        self.client.force_login(self.user)
        batch=Batch.objects.create(owner=self.user,name='Решения',checks=['sto'],status='completed')
        WorkerRun.objects.create(batch=batch,worker='desktop',state='completed',local_id='local',report={'findings':[{'id':'finding-1','status':'confirmed'}]})
        url=f'/normcontol/batches/{batch.pk}/findings/finding-1/disposition/'
        response=self.client.post(url,data={'state':'in_work','comment':''},content_type='application/json')
        self.assertEqual(response.status_code,200);self.assertEqual(FindingDisposition.objects.get().state,'in_work')
        response=self.client.post(url,data={'state':'disputed','comment':'слишком коротко'},content_type='application/json')
        self.assertEqual(response.status_code,200);self.assertEqual(FindingDisposition.objects.get().state,'disputed')
    def test_public_health_reflects_llm_worker_state(self):
        self.assertFalse(self.client.get('/normcontol/health/').json()['llm_online'])
        worker=WorkerPresence.objects.create(name='desktop',state='idle')
        self.assertTrue(self.client.get('/normcontol/health/').json()['llm_online'])
        worker.state='error';worker.save()
        self.assertFalse(self.client.get('/normcontol/health/').json()['llm_online'])
    def test_failed_batch_can_be_requeued(self):
        self.client.force_login(self.user)
        batch=Batch.objects.create(owner=self.user,name='Повтор',checks=['sto'],status='failed')
        Document.objects.create(batch=batch,name='test.docx',file=document(),size=200,sha256='a'*64)
        WorkerRun.objects.create(batch=batch,worker='desktop',state='failed',report={'status':'Ошибка'})
        response=self.client.post(f'/normcontol/batches/{batch.pk}/action/',{'action':'retry'})
        self.assertEqual(response.status_code,302)
        batch.refresh_from_db();self.assertEqual(batch.status,'failed')
        self.assertEqual(batch.worker_run.report,{'status':'Ошибка'})
        repeated=batch.reruns.get();self.assertEqual(repeated.status,'waiting')
        self.assertEqual(repeated.documents.get().file.name,batch.documents.get().file.name)
        self.assertEqual(repeated.checks,batch.checks)
        again=self.client.post(f'/normcontol/batches/{batch.pk}/action/',{'action':'rerun'})
        self.assertEqual(again.url,response.url);self.assertEqual(batch.reruns.count(),1)
    def test_repeat_terminal_states_and_access(self):
        self.client.force_login(self.user)
        for status in ('completed','partial','cancelled'):
            b=Batch.objects.create(owner=self.user,name='Повтор',status=status,checks=['logic'])
            Document.objects.create(batch=b,name='test.docx',file=document(),size=200,sha256='a'*64)
            self.assertContains(self.client.get(f'/normcontol/batches/{b.pk}/'),'Повторный нормоконтроль')
            self.assertEqual(self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'rerun'}).status_code,302)
            self.assertEqual(b.reruns.count(),1)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'rerun'}).status_code,404)
        self.client.force_login(self.user)
        b.status='running';b.save()
        self.assertEqual(self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'rerun'}).status_code,403)
    def test_repeat_requires_files_and_worker_receives_fresh_flag(self):
        self.client.force_login(self.user);b=Batch.objects.create(owner=self.user,name='Повтор',status='completed',checks=['sto'])
        self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'rerun'})
        self.assertFalse(b.reruns.exists())
        Document.objects.create(batch=b,name='test.docx',file=document(),size=200,sha256='a'*64)
        self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'rerun'})
        with patch.dict(os.environ,{'NORMCONTROL_WORKER_TOKEN':'x'*40}):
            claim=self.client.post('/normcontol/worker/claim/',data={'worker':'test'},content_type='application/json',HTTP_AUTHORIZATION='Bearer '+'x'*40)
        self.assertTrue(claim.json()['fresh_review']);self.assertEqual(claim.json()['job'],str(b.reruns.get().pk))
    def test_admin_delete_permissions_and_confirmation(self):
        b=Batch.objects.create(owner=self.user,name='Удаление',status='completed')
        d=Document.objects.create(batch=b,name='test.docx',file=document(),size=200)
        path=d.file.path
        WorkerRun.objects.create(batch=b,worker='test',state='completed',report={'ok':True})
        url=f'/normcontol/batches/{b.pk}/action/'
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(url,{'action':'delete','confirm_delete':str(b.pk)}).status_code,403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(f'/normcontol/batches/{b.pk}/delete/'),'Удалить пакет?')
        self.assertTrue(Batch.objects.filter(pk=b.pk).exists())
        self.assertEqual(self.client.post(url,{'action':'delete'}).status_code,403)
        self.assertEqual(self.client.post(url,{'action':'delete','confirm_delete':str(b.pk)}).status_code,302)
        self.assertFalse(Batch.objects.filter(pk=b.pk).exists());self.assertFalse(os.path.exists(path))
        self.assertFalse(WorkerRun.objects.exists())
    def test_delete_keeps_shared_repeat_files(self):
        b=Batch.objects.create(owner=self.user,name='Исходный',status='completed')
        d=Document.objects.create(batch=b,name='test.docx',file=document(),size=200)
        path=d.file.path
        r=Batch.objects.create(owner=self.user,name='Повтор',source_batch=b,fresh_review=True,status='completed')
        Document.objects.create(batch=r,name=d.name,file=d.file.name,size=d.size)
        self.client.force_login(self.admin)
        self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'delete','confirm_delete':str(b.pk)})
        r.refresh_from_db();self.assertIsNone(r.source_batch_id);self.assertTrue(r.fresh_review)
        self.assertTrue(os.path.exists(path))
        self.client.post(f'/normcontol/batches/{r.pk}/action/',{'action':'delete','confirm_delete':str(r.pk)})
        self.assertFalse(os.path.exists(path))
    def test_delete_blocks_active_worker_even_after_cancel_request(self):
        self.client.force_login(self.admin)
        for status in ('waiting','preparing','running','paused','cancelled'):
            b=Batch.objects.create(owner=self.user,name='Активный',status=status)
            WorkerRun.objects.create(batch=b,worker='test',state='running')
            self.client.post(f'/normcontol/batches/{b.pk}/action/',{'action':'delete','confirm_delete':str(b.pk)})
            self.assertTrue(Batch.objects.filter(pk=b.pk).exists())
    def test_self_disable_blocked_and_login_throttled(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f'/normcontol/settings/users/{self.admin.pk}/toggle/').status_code,403)
        self.client.logout()
        for i in range(8):self.client.post('/normcontol/login/',{'username':'owner','password':'bad'})
        self.assertContains(self.client.post('/normcontol/login/',{'username':'owner','password':'bad'}),'Слишком много попыток')

    def test_rzdtech_empty_password_starts_registration(self):
        response=self.client.post('/normcontol/login/',{'username':'RZDTECH/new.user','password':''})
        self.assertRedirects(response,'/normcontol/register/')
        page=self.client.get('/normcontol/register/')
        self.assertContains(page,'RZDTECH/new.user');self.assertContains(page,'Повторите пароль')

    def test_rzdtech_registration_creates_and_logs_in_user(self):
        self.client.post('/normcontol/login/',{'username':'RZDTECH/new.user','password':''})
        password='Railway-Document-2026!'
        response=self.client.post('/normcontol/register/',{'password1':password,'password2':password})
        self.assertRedirects(response,'/normcontol/')
        user=User.objects.get(username='RZDTECH/new.user')
        self.assertTrue(user.check_password(password));self.assertEqual(int(self.client.session['_auth_user_id']),user.pk)
        self.assertFalse(AccessProfile.objects.get(user=user).must_change_password)

    def test_registration_rejects_mismatch_existing_and_non_rzdtech(self):
        response=self.client.post('/normcontol/login/',{'username':'external','password':''})
        self.assertEqual(response.status_code,200);self.assertFalse(User.objects.filter(username='external').exists())
        self.client.post('/normcontol/login/',{'username':'RZDTECH/test.user','password':''})
        response=self.client.post('/normcontol/register/',{'password1':'Railway-Document-2026!','password2':'Different-Password-2026!'})
        self.assertContains(response,'Пароли не совпадают');self.assertFalse(User.objects.filter(username='RZDTECH/test.user').exists())
        User.objects.create_user('RZDTECH/existing',password='Railway-Document-2026!')
        response=self.client.post('/normcontol/login/',{'username':'RZDTECH/existing','password':''})
        self.assertContains(response,'уже зарегистрирована')

    def test_admin_can_reset_user_password(self):
        self.client.force_login(self.admin);new_password='Reset-Railway-Password-2026!'
        response=self.client.post(f'/normcontol/settings/users/{self.user.pk}/password/',{'new_password1':new_password,'new_password2':new_password})
        self.assertRedirects(response,'/normcontol/settings/users/')
        self.user.refresh_from_db();self.assertTrue(self.user.check_password(new_password))
        self.assertTrue(AccessProfile.objects.get(user=self.user).must_change_password)

    def test_admin_reorders_and_cancels_queue(self):
        first=Batch.objects.create(owner=self.user,name='Первый',status='waiting',queue_position=1)
        second=Batch.objects.create(owner=self.other,name='Второй',status='waiting',queue_position=2)
        self.client.force_login(self.user);self.assertEqual(self.client.get('/normcontol/settings/queue/').status_code,302)
        self.client.force_login(self.admin);self.assertContains(self.client.get('/normcontol/settings/queue/'),'Очередь проверок')
        self.client.post(f'/normcontol/settings/queue/{second.pk}/action/',{'action':'top'})
        second.refresh_from_db();first.refresh_from_db();self.assertLess(second.queue_position,first.queue_position)
        self.client.post(f'/normcontol/settings/queue/{second.pk}/action/',{'action':'cancel'})
        second.refresh_from_db();self.assertEqual(second.status,'cancelled')
