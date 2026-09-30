"""Canonical launch, durable history, strict ownership and truthful exports."""
import io,json,tempfile,uuid
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import AccessProfile,Batch,Document,WorkerRun,Conversation,ChatMessage,LaunchReceipt
from .tests import document
from .chat import intent

@override_settings(KNOWLEDGE_V2_ENABLED=False)
class ChatTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('chat-owner');self.other=User.objects.create_user('other-chat-owner')
        self.client.force_login(self.user)
        self.temp=tempfile.TemporaryDirectory();self.setting=override_settings(MEDIA_ROOT=self.temp.name);self.setting.enable()
        self.addCleanup(self.setting.disable);self.addCleanup(self.temp.cleanup)
        self.c=Conversation.objects.create(owner=self.user);self.url=f'/normcontol/chat/conversations/{self.c.pk}/';self.key=str(uuid.uuid4())
    def post(self,**changes):
        data={'text':'Проверь грамматику и логику','key':self.key,'config':json.dumps({'roles':['target'],'checks':['logic','language']}),'documents':[document('Проверка.docx')]}
        data.update(changes);return self.client.post(self.url+'send/',data)
    def test_default_is_chat_without_register_or_telemetry(self):
        r=self.client.get('/normcontol/')
        self.assertTemplateUsed(r,'chat.html');self.assertContains(r,'role="switch"');self.assertNotContains(r,'app.js');self.assertNotContains(r,'data-llm-status')
        self.assertFalse(AccessProfile.objects.get(user=self.user).is_expert)
    def test_full_interface_remains_available(self):
        self.assertTemplateUsed(self.client.get('/normcontol/?presentation=expert'),'dashboard.html')
    def test_expert_new_check_has_prompt_and_saves_same_run_conversation(self):
        self.assertContains(self.client.get('/normcontol/batches/new/?embedded=1'),'Промпт пользователя')
        key=str(uuid.uuid4());text='Проверь грамматику документа'
        payload={'checks':['language'],'launch_key':key,'user_prompt':text,'documents':[document('Проверка.docx')]}
        result=self.client.post('/normcontol/batches/new/',payload,HTTP_X_REQUESTED_WITH='XMLHttpRequest');self.assertEqual(result.status_code,200,result.content)
        b=Batch.objects.get();c=Conversation.objects.get(owner=self.user,batch=b);self.assertEqual(c.messages.get(key=key).text,text)
        payload['documents']=[document('Проверка.docx')];self.assertEqual(self.client.post('/normcontol/batches/new/',payload,HTTP_X_REQUESTED_WITH='XMLHttpRequest').status_code,200)
        self.assertEqual(Batch.objects.count(),1);self.assertEqual(c.messages.count(),1)
    def test_preference_does_not_grant_rights(self):
        result=self.client.post('/normcontol/chat/preferences/',json.dumps({'presentation':'expert','theme':'light'}),content_type='application/json')
        self.assertEqual(result.status_code,200);profile=AccessProfile.objects.get(user=self.user)
        self.assertEqual(profile.presentation,'expert');self.assertFalse(profile.is_expert);self.assertFalse(profile.can_view_others)
        self.assertEqual(self.client.get('/normcontol/settings/users/').status_code,302)
        self.client.post('/normcontol/chat/preferences/',json.dumps({'theme':'dark'}),content_type='application/json');profile.refresh_from_db();self.assertEqual(profile.presentation,'expert')
    def test_one_real_queue_launch_and_retries(self):
        first=self.post();self.assertEqual(first.status_code,200,first.content)
        b=Batch.objects.get();self.assertEqual(b.status,'waiting');self.assertEqual(b.checks,['logic','language']);self.assertEqual(b.documents.get().review_role,'target')
        second=self.post();self.assertEqual(second.status_code,200);self.assertEqual(Batch.objects.count(),1);self.assertEqual(LaunchReceipt.objects.count(),1)
        self.assertEqual(ChatMessage.objects.filter(conversation=self.c,role='user').count(),1)
        self.assertEqual(ChatMessage.objects.filter(conversation=self.c,metadata__kind='progress').count(),1)
    def test_canonical_chat_and_form_same_directions_roles_hashes(self):
        self.post();b=Batch.objects.get()
        result=self.client.post('/normcontol/batches/new/',{'checks':['language','logic'],'launch_key':str(uuid.uuid4()),'documents':[document('Проверка.docx')],'document_roles':'["target"]'},HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(result.status_code,200,result.content);other=Batch.objects.exclude(pk=b.pk).get()
        self.assertEqual(b.checks,other.checks);self.assertEqual(b.documents.get().sha256,other.documents.get().sha256);self.assertEqual(b.documents.get().review_role,other.documents.get().review_role)
    def test_role_ambiguity_one_saved_question_then_same_draft(self):
        r=self.post(config='{}');self.assertEqual(r.status_code,422);self.assertIn('clarification',r.json());self.assertEqual(Batch.objects.count(),0)
        self.post(config='{}');self.assertEqual(ChatMessage.objects.filter(metadata__kind='clarification').count(),1)
        self.assertEqual(self.post().status_code,200);self.assertEqual(Batch.objects.count(),1)
    def test_reference_scope_stored_and_claim_requires_capability(self):
        r=self.post(config=json.dumps({'checks':['logic'],'roles':['target','approved_reference']}),documents=[document('ЧТЗ.docx'),document('ТЗ.docx',text='Основание проверки')])
        self.assertEqual(r.status_code,200,r.content)
        batch=Batch.objects.get();self.assertEqual(list(batch.documents.order_by('id').values_list('review_role',flat=True)),['target','approved_reference'])
    def test_same_content_cannot_have_conflicting_roles(self):
        r=self.post(config=json.dumps({'checks':['logic'],'roles':['target','approved_reference']}),documents=[document('ЧТЗ.docx'),document('Копия.docx')])
        self.assertEqual(r.status_code,422);self.assertFalse(Batch.objects.exists())
    def test_roles_frozen_at_launch_and_word_cannot_reconfigure_analysis(self):
        self.post();b=Batch.objects.get();d=b.documents.get()
        r=self.client.post(f'/normcontol/batches/{b.pk}/word-review/',{'action':'roles','role_'+str(d.pk):'approved_reference'})
        self.assertContains(r,'Роли закреплены');d.refresh_from_db();self.assertEqual(d.review_role,'target')
    def test_malformed_structured_input_does_not_execute_or_launch(self):
        r=self.client.post('/normcontol/chat/preferences/','not json',content_type='application/json');self.assertEqual(r.status_code,400)
        r=self.post(config='["arbitrary-command"]');self.assertEqual(r.status_code,400);self.assertFalse(Batch.objects.exists())
    def test_no_base_explicitly_unavailable_no_silent_launch(self):
        r=self.post(text='Проверь по СТО',config=json.dumps({'roles':['target']}));self.assertEqual(r.status_code,422);self.assertIn('СТО недоступна',r.json()['clarification']);self.assertFalse(Batch.objects.exists())
    def test_status_and_details_do_not_launch(self):
        for text in ('Какой статус?','Покажи замечания','Скачать Word'):
            r=self.post(text=text,key=str(uuid.uuid4()),documents=[]);self.assertEqual(r.status_code,200)
        self.assertFalse(Batch.objects.exists());self.assertIn('Эксперт',str(self.client.get(self.url).json()))
    def test_cross_owner_history_and_files_denied(self):
        self.post();self.client.force_login(self.other)
        for path in (self.url,self.url+'export/summary/',self.url+'export/xlsx/'):
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.get('/normcontol/chat/conversations/').json()['items'],[])
    def test_shared_viewer_has_own_conversation_without_control_rights(self):
        self.post();b=Batch.objects.get();self.client.force_login(self.other)
        AccessProfile.objects.create(user=self.other,can_view_others=True)
        r=self.client.post('/normcontol/chat/conversations/',json.dumps({'batch':str(b.pk)}),content_type='application/json');self.assertEqual(r.status_code,201)
        shared=Conversation.objects.get(pk=r.json()['id']);self.assertEqual(shared.owner,self.other);self.assertEqual(shared.batch,b)
        self.assertNotEqual(shared.pk,self.c.pk);self.assertEqual(r.json()['state']['actions'],[])
        result=self.client.post(f'/normcontol/chat/conversations/{shared.pk}/control/',json.dumps({'action':'cancel'}),content_type='application/json');self.assertEqual(result.status_code,403)
    def test_direct_word_link_keeps_bottom_switch_and_same_batch(self):
        self.post();b=Batch.objects.get()
        r=self.client.get(f'/normcontol/batches/{b.pk}/word-review/',HTTP_SEC_FETCH_DEST='document')
        self.assertTemplateUsed(r,'chat.html');self.assertContains(r,'role="switch"');self.assertIn(str(b.pk),r.context['initial']['expert_url'])
    def test_draft_restored_without_document_bytes(self):
        r=self.client.post(self.url,json.dumps({'text':'черновик','roles':['target'],'file_content':'private-bytes','path':'C:/secret'}),content_type='application/json')
        self.assertEqual(r.status_code,200);self.c.refresh_from_db();self.assertEqual(self.c.draft,{'text':'черновик','roles':['target']})
    def test_foreign_html_is_data_and_not_markup(self):
        self.post(text='<script>alert(1)</script> статус',documents=[])
        page=self.client.get('/normcontol/chat/?conversation='+str(self.c.pk));self.assertNotContains(page,'<script>alert(1)</script>');self.assertContains(page,'\\u003Cscript\\u003E')
    def test_partial_zero_is_not_compliance(self):
        b=Batch.objects.create(owner=self.user,name='Частичный',checks=['logic'],status='partial');self.c.batch=b;self.c.save()
        WorkerRun.objects.create(batch=b,state='partial',report={'findings':[],'limitations':['Не прочитано изображение']})
        r=self.client.get(self.url).json();self.assertFalse(r['state']['complete']);self.assertIn('неполным',r['state']['text'])
    def test_running_no_file_or_false_ready(self):
        self.post();r=self.client.get(self.url).json();self.assertFalse(r['state']['terminal']);self.assertEqual(r['state']['exports'],[])
        self.assertEqual(self.client.get(self.url+'export/xlsx/').status_code,409)
    def test_full_excel_ignores_pagination_filters(self):
        b=Batch.objects.create(owner=self.user,name='Полный',checks=['logic'],status='completed');self.c.batch=b;self.c.save()
        fs=[{'id':str(i),'status':'confirmed','issue':'Замечание '+str(i),'evidence':[],'suggestion':''} for i in range(105)]
        WorkerRun.objects.create(batch=b,state='completed',report={'findings':fs})
        r=self.client.get(self.url+'export/xlsx/?status=question&page=3');self.assertEqual(r.status_code,200)
        from zipfile import ZipFile
        with ZipFile(io.BytesIO(r.content)) as z:self.assertIn('Замечание 104',''.join(z.read(n).decode() for n in z.namelist() if n.startswith('xl/worksheets/')))
    def test_invalid_document_cannot_launch(self):
        r=self.post(documents=[SimpleUploadedFile('bad.docx',b'not zip')]);self.assertEqual(r.status_code,422);self.assertFalse(Batch.objects.exists())
    def test_cancel_is_canonical_and_history_survives(self):
        self.post();r=self.client.post(self.url+'control/',json.dumps({'action':'cancel'}),content_type='application/json');self.assertEqual(r.status_code,200)
        self.assertEqual(Batch.objects.get().status,'cancelled');self.assertTrue(ChatMessage.objects.exists())
    def test_new_conversation_creation_idempotent(self):
        key=str(uuid.uuid4());a=self.client.post('/normcontol/chat/conversations/',json.dumps({'key':key}),content_type='application/json').json()
        b=self.client.post('/normcontol/chat/conversations/',json.dumps({'key':key}),content_type='application/json').json();self.assertEqual(a['id'],b['id'])
    def test_natural_request_has_expected_directions(self):
        self.assertEqual(intent('Проверь ЧТЗ по СТО, грамматике и логике; утверждённое ТЗ используй как основание'),['sto','logic','language'])
        self.assertEqual(intent('Какой статус проверки?'),'status')
