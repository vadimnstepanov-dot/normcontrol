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
    def test_theme_switch_is_available_on_private_and_login_pages(self):
        login=self.client.get('/normcontol/login/')
        self.assertContains(login,'data-theme-toggle');self.assertContains(login,'theme.js')
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/normcontol/'),'data-theme-toggle')
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
    def test_selecting_history_batch_targets_its_progress_and_register(self):
        self.client.force_login(self.user)
        current=Batch.objects.create(owner=self.user,name='Новая проверка',status='running',checks=['sto'])
        previous=Batch.objects.create(owner=self.user,name='Прошлая проверка',status='completed',checks=['sto'])
        WorkerRun.objects.create(batch=current,worker='desktop',state='running',snapshot={'stages':{'sto':{'done':1,'pending':3}}})
        WorkerRun.objects.create(batch=previous,worker='desktop',state='completed',snapshot={'stages':{'sto':{'done':4}}},report={'findings':[{'id':'older-result','issue':'Найдено в прошлой проверке','status':'confirmed'}]})
        response=self.client.get('/normcontol/',{'batch':previous.pk})
        self.assertEqual(response.context['selected_batch'].pk,previous.pk)
        self.assertContains(response,f'?batch={previous.pk}#current-review')
        self.assertContains(response,f'/normcontol/batches/{previous.pk}/status/')
        self.assertContains(response,f'/normcontol/batches/{previous.pk}/register/')
        self.assertContains(response,'Найдено в прошлой проверке')
        self.assertContains(response,'id="current-review-button-state">Завершён')
        self.assertContains(response,'active-row')
    def test_prepared_batch_is_selectable_with_correct_status(self):
        self.client.force_login(self.user)
        batch=Batch.objects.create(owner=self.user,name='Ожидает запуска',status='prepared',checks=['sto'])
        response=self.client.get('/normcontol/',{'batch':batch.pk})
        self.assertContains(response,'id="current-review-button"')
        self.assertContains(response,f'?batch={batch.pk}#current-review')
        data=self.client.get(f'/normcontol/batches/{batch.pk}/status/').json()
        self.assertEqual(data['state'],'prepared')
        self.assertFalse(data['report_available'])
        register=self.client.get(f'/normcontol/batches/{batch.pk}/register/').json()
        self.assertEqual(register['records'],[])
        self.assertFalse(register['report_available'])
    def test_batch_history_collapses_to_three_and_expands_for_search_or_selection(self):
        self.client.force_login(self.user)
        for n in range(5):Batch.objects.create(owner=self.user,name=f'Пакет {n}',status='completed')
        page=self.client.get('/normcontol/')
        self.assertFalse(page.context['expand_batches'])
        self.assertEqual(page.context['hidden_batch_count'],2)
        self.assertContains(page,'Показаны 3 из 5 проверок')
        self.assertContains(page,'data-history-extra hidden',count=2)
        self.assertContains(page,'aria-expanded="false"')
        search=self.client.get('/normcontol/',{'q':'Пакет'})
        self.assertTrue(search.context['expand_batches'])
        self.assertContains(search,'Показаны все 5 проверок')
        self.assertNotContains(search,'data-history-extra hidden')
        older_id=page.context['batches'][3].pk
        older=self.client.get('/normcontol/',{'batch':older_id})
        self.assertEqual(older.context['selected_batch'].pk,older_id)
        self.assertTrue(older.context['expand_batches'])
        self.assertContains(older,'aria-expanded="true"')
    def test_dashboard_has_clickable_register_stats_and_errors(self):
        self.client.force_login(self.user)
        rag={'catalog':'catalog-1','requirements':850,'unresolved_dependencies':9,'settings':{'requirements_per_group':10,'evidence_chars_per_group':52000,'search_normalization':'Русская морфология'},'sources':[{'name':'СТО РЖД 04.001.1–2021','sha256':'abc123','blocks':100,'tables':4,'warnings':1}]}
        WorkerPresence.objects.create(name='desktop',state='busy',details={'rag':rag})
        batch=Batch.objects.create(owner=self.user,name='С ошибкой',status='running',checks=['sto'])
        WorkerRun.objects.create(batch=batch,worker='desktop',state='running',snapshot={'task_errors':[{'id':'task-1','stage':'sto','state':'failed','attempts':2,'error':'Неверный JSON'}]})
        response=self.client.get('/normcontol/')
        self.assertContains(response,'data-register-filter="confirmed"');self.assertContains(response,'data-register-filter="task-error"')
        self.assertContains(response,'id="finding-type"');self.assertContains(response,'id="register-page-label"')
        self.assertContains(response,'data-export-format="xlsx"');self.assertContains(response,'data-export-format="docx"')
        self.assertContains(response,'Реестр замечаний и ошибок');self.assertContains(response,'href="/normcontol/rag/"')
        self.assertNotContains(response,'СТО РЖД 04.001.1–2021');self.assertNotContains(response,'catalog-1')
        rag_response=self.client.get('/normcontol/rag/')
        self.assertContains(rag_response,'Нормативная база');self.assertContains(rag_response,'СТО РЖД 04.001.1–2021');self.assertContains(rag_response,'catalog-1')
        self.assertContains(rag_response,'nav-link active')
    def test_invalid_upload_is_atomic(self):
        self.client.force_login(self.user)
        response=self.client.post('/normcontol/batches/new/',{'name':'Broken','profile':'chtz','checks':['sto'],'documents':[document(),SimpleUploadedFile('fake.docx',b'not a zip')]})
        self.assertEqual(response.status_code,200);self.assertEqual(Batch.objects.count(),0)
        self.assertContains(response,'не является корректным')
    def test_add_documents_after_stop_creates_revision_and_preserves_report(self):
        self.client.force_login(self.user)
        source=Batch.objects.create(owner=self.user,name='Остановленный пакет',status='cancelled',checks=['sto','logic'])
        original=document('first.docx');meta={'sha256':'a'*64,'size':original.size}
        first=Document.objects.create(batch=source,name='first.docx',file=original,**meta)
        WorkerRun.objects.create(batch=source,worker='desktop',state='cancelled',report={'findings':[{'id':'old'}]})
        source_page=self.client.get(f'/normcontol/batches/{source.pk}/')
        self.assertContains(source_page,'Проверка будет перезапущена с начала');self.assertContains(source_page,'name="confirm_restart"')
        response=self.client.post(f'/normcontol/batches/{source.pk}/documents/add/',{'confirm_restart':'1','documents':[document('second.docx')]})
        self.assertEqual(response.status_code,302)
        revision=source.reruns.get();self.assertEqual(revision.status,'waiting');self.assertGreater(revision.queue_position,0);self.assertTrue(revision.fresh_review);self.assertEqual(revision.documents.count(),2)
        self.assertEqual(set(revision.documents.values_list('name',flat=True)),{'first.docx','second.docx'})
        self.assertEqual(source.documents.count(),1);self.assertEqual(source.worker_run.report,{'findings':[{'id':'old'}]})
        self.assertRedirects(response,f'/normcontol/batches/{revision.pk}/')
        page=self.client.get(f'/normcontol/batches/{revision.pk}/');self.assertContains(page,'Ожидает локального обработчика')
        self.assertEqual(revision.documents.get(name='first.docx').file.name,first.file.name)
    def test_add_documents_rejects_active_foreign_and_invalid_requests(self):
        active=Batch.objects.create(owner=self.user,name='Активный',status='running',checks=['sto'])
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(f'/normcontol/batches/{active.pk}/documents/add/',{'confirm_restart':'1','documents':[document()]}).status_code,403)
        stopped=Batch.objects.create(owner=self.user,name='Остановленный',status='cancelled',checks=['sto'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(f'/normcontol/batches/{stopped.pk}/documents/add/',{'confirm_restart':'1','documents':[document()]}).status_code,404)
        self.client.force_login(self.user)
        response=self.client.post(f'/normcontol/batches/{stopped.pk}/documents/add/',{'confirm_restart':'1','documents':[SimpleUploadedFile('bad.docx',b'broken')]})
        self.assertRedirects(response,f'/normcontol/batches/{stopped.pk}/');self.assertFalse(stopped.reruns.exists())
        response=self.client.post(f'/normcontol/batches/{stopped.pk}/documents/add/',{'documents':[document()]})
        self.assertRedirects(response,f'/normcontol/batches/{stopped.pk}/');self.assertFalse(stopped.reruns.exists())
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
        for path in ('/normcontol/','/normcontol/batches/new/','/normcontol/reports/','/normcontol/rag/','/normcontol/settings/llm/','/normcontol/settings/users/','/normcontol/settings/audit/','/normcontol/account/password/'):
            response=self.client.get(path);self.assertEqual(response.status_code,200,path)
    def test_dashboard_has_live_workspace_controls(self):
        self.client.force_login(self.admin)
        batch=Batch.objects.create(owner=self.admin,name='Живая проверка',checks=['sto'],status='running')
        WorkerRun.objects.create(batch=batch,worker='desktop',state='running',local_id='local',snapshot={'stages':{'sto':{'done':2,'pending':1}},'findings':{'confirmed':1}},report={'findings':[{'id':'f1','status':'confirmed','severity':'major','category':'СТО','issue':'Проверка интерфейса','explanation':'Описание','evidence':[],'suggestion':'Исправить'}]})
        response=self.client.get('/normcontol/',{'batch':batch.pk})
        self.assertContains(response,'ТЕКУЩАЯ ПРОВЕРКА')
        self.assertContains(response,'Реестр замечаний и ошибок')
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

    def test_admin_controls_read_only_access_to_other_users_checks(self):
        batch=Batch.objects.create(owner=self.other,name='Чужой пакет',status='completed',checks=['sto'])
        doc=Document.objects.create(batch=batch,name='other.docx',file=document('other.docx'),size=100,sha256='a'*64)
        WorkerRun.objects.create(batch=batch,worker='test',state='completed',report={'findings':[{'id':'finding-1','status':'confirmed','issue':'Тестовое замечание','category':'СТО','evidence':[]}]})
        detail=f'/normcontol/batches/{batch.pk}/'
        report=f'/normcontol/batches/{batch.pk}/report/'
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(detail).status_code,404)
        self.assertEqual(self.client.get(report).status_code,404)
        self.assertEqual(self.client.post(f'/normcontol/settings/users/{self.user.pk}/view-others/').status_code,302)

        self.client.force_login(self.admin)
        setting=f'/normcontol/settings/users/{self.user.pk}/view-others/'
        self.assertContains(self.client.get('/normcontol/settings/users/'),'Видеть чужие проверки')
        self.assertRedirects(self.client.post(setting),'/normcontol/settings/users/')
        self.assertTrue(AccessProfile.objects.get(user=self.user).can_view_others)

        self.client.force_login(self.user)
        self.assertContains(self.client.get('/normcontol/'),'Чужой пакет')
        self.assertContains(self.client.get('/normcontol/reports/'),'Чужой пакет')
        page=self.client.get(detail)
        self.assertContains(page,'Чужой пакет')
        self.assertNotContains(page,'Повторный нормоконтроль')
        self.assertEqual(self.client.get(f'/normcontol/documents/{doc.pk}/download/').status_code,200)
        self.assertEqual(self.client.get(report).status_code,200)
        self.assertEqual(self.client.get(f'/normcontol/batches/{batch.pk}/register/').status_code,200)
        self.assertEqual(self.client.get(f'/normcontol/batches/{batch.pk}/status/').status_code,200)
        self.assertEqual(self.client.post(f'/normcontol/batches/{batch.pk}/action/',{'action':'archive'}).status_code,404)
        self.assertEqual(self.client.post(f'/normcontol/batches/{batch.pk}/wake/').status_code,404)
        self.assertEqual(self.client.post(f'/normcontol/batches/{batch.pk}/feedback/',data={'finding_id':'finding-1','comment':'Проверить отдельно'},content_type='application/json').status_code,404)
        self.assertEqual(self.client.post(f'/normcontol/batches/{batch.pk}/findings/finding-1/disposition/',data={'state':'fixed'},content_type='application/json').status_code,404)
        self.client.force_login(self.admin)
        self.client.post(setting)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(detail).status_code,404)

    def test_admin_can_assign_and_remove_administrator_role(self):
        self.client.force_login(self.user)
        path=f'/normcontol/settings/users/{self.other.pk}/admin/'
        self.assertEqual(self.client.post(path).status_code,302)
        self.other.refresh_from_db();self.assertFalse(self.other.is_staff)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(path).status_code,302)
        self.other.refresh_from_db();self.assertTrue(self.other.is_staff)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/normcontol/settings/users/').status_code,200)
        self.assertEqual(self.client.post(f'/normcontol/settings/users/{self.other.pk}/admin/').status_code,403)
        self.assertEqual(self.client.post(f'/normcontol/settings/users/{self.user.pk}/view-others/').status_code,302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(path).status_code,302)
        self.other.refresh_from_db();self.assertFalse(self.other.is_staff)
        self.assertEqual(self.client.post(f'/normcontol/settings/users/{self.admin.pk}/admin/').status_code,403)
