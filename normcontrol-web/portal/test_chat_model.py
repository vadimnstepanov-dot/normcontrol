"""Real queue persistence, canonical intake isolation, access and model transport."""
import json,os,uuid
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from .models import Conversation,ChatResponse,ChatMessage,Batch,LLMConfig,LLMRuntime,WorkerRun
from . import chat_model as model
from .model_demand import pending_demand,queue_start
from .tests import document

@override_settings(CHAT_MODEL_BACKGROUND=False,KNOWLEDGE_V2_ENABLED=False)
class DialogueTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('dialogue-owner');self.other=User.objects.create_user('other-dialogue')
        self.client.force_login(self.user);self.c=Conversation.objects.create(owner=self.user)
        self.url=f'/normcontol/chat/conversations/{self.c.pk}/';self.key=str(uuid.uuid4())
        self.config=LLMConfig.objects.create(endpoint='http://127.0.0.1:8098',context_tokens=4096,output_tokens=256)
        self.paths=[];self.generation=None
    def send(self,text='сколько будет 2+2',**kwargs):
        values={'key':self.key,'text':text,'config':'{}'};values.update(kwargs)
        return self.client.post(self.url+'send/',values)
    def row(self):return ChatResponse.objects.get()
    def transport(self,gateway,path,value=None,timeout=15):
        self.paths.append(path)
        if path=='/pipeline/ticket':
            self.assertEqual(value['stage'],'chat');self.assertEqual(value['ram_mb'],128)
            return {'id':'fixture-ticket','state':'running'} if value['action']=='acquire' else {'released':True}
        if path=='/health':return {'status':'ok'}
        if path=='/model/profile':self.assertEqual(value,{'profile':'text'});return {'profile':'text'}
        if path=='/props':return {'default_generation_settings':{'n_ctx':4096}}
        if path=='/v1/models':return {'data':[{'id':'fixture-qwen'}]}
        if path=='/apply-template':return {'prompt':json.dumps(value['messages'],ensure_ascii=False)}
        if path=='/tokenize':return {'tokens':list(range(len(value['content'])//3))}
        if path=='/v1/chat/completions':
            self.generation=value;return {'choices':[{'message':{'content':'2 + 2 = 4.'},'finish_reason':'stop'}],'usage':{'prompt_tokens':200,'completion_tokens':8}}
        self.fail(path)
    def execute(self):
        item=model.claim();self.assertIsNotNone(item)
        with patch.object(model.Gateway,'http',autospec=True,side_effect=self.transport):model.execute(*item)
    def test_plain_question_creates_one_durable_model_turn_not_a_batch(self):
        result=self.send();self.assertEqual(result.status_code,200,result.content)
        self.assertEqual(self.row().state,'queued');self.assertFalse(Batch.objects.exists())
        self.assertEqual(result.json()['messages'][-1]['metadata']['kind'],'model')
        self.assertEqual(self.send().status_code,200);self.assertEqual(ChatResponse.objects.count(),1)
        self.c.refresh_from_db();self.assertEqual(self.c.title,'сколько будет 2+2')
    def test_explicit_dialogue_can_contain_review_verbs(self):
        self.assertEqual(self.send('Проверь расчёт 2+2',config=json.dumps({'mode':'dialogue'})).status_code,200)
        self.assertTrue(ChatResponse.objects.exists());self.assertFalse(Batch.objects.exists())
    def test_invalid_mode_and_external_endpoint_do_not_control_connection(self):
        self.assertEqual(self.send(config=json.dumps({'mode':'alien'})).status_code,400)
        self.assertEqual(self.send(config=json.dumps({'endpoint':'http://untrusted.test'})).status_code,200)
        self.execute();self.assertEqual(self.generation['model'],'fixture-qwen')
    def test_review_without_file_still_clarifies(self):
        self.assertEqual(self.send('Проверь грамматику').status_code,422)
        self.assertFalse(ChatResponse.objects.exists());self.assertFalse(Batch.objects.exists())
    def test_dialogue_with_files_does_not_silently_discard_attachment(self):
        result=self.send(config=json.dumps({'mode':'dialogue'}),documents=[document('fixture.docx')])
        self.assertEqual(result.status_code,422);self.assertIn('без вложений',result.json()['clarification'])
        self.assertFalse(ChatResponse.objects.exists())
        # Same key from a clarification may be resolved as a dialogue without files.
        self.assertEqual(self.send(config=json.dumps({'mode':'dialogue'})).status_code,200)
        self.assertEqual(self.c.messages.filter(role='user').count(),1)
    def test_only_one_pending_turn_per_conversation(self):
        self.send();self.key=str(uuid.uuid4())
        self.assertEqual(self.send('Второй вопрос').status_code,409);self.assertEqual(ChatResponse.objects.count(),1)
    def test_model_uses_actual_context_and_shared_gpu_then_saves_plain_reply(self):
        self.send();self.execute();row=self.row();self.assertEqual(row.state,'done')
        self.assertEqual(row.assistant_message.text,'2 + 2 = 4.')
        self.assertEqual(row.assistant_message.metadata['usage']['completion_tokens'],8)
        self.assertFalse(self.generation['stream']);self.assertNotIn('response_format',self.generation)
        self.assertLess(self.paths.index('/pipeline/ticket'),self.paths.index('/v1/chat/completions'))
        self.assertEqual(self.paths[-1],'/pipeline/ticket')
        self.assertFalse(Batch.objects.exists());self.assertFalse(WorkerRun.objects.exists())
    def test_model_dialogue_keeps_review_state_and_results(self):
        batch=Batch.objects.create(owner=self.user,name='fixture',status='running',checks=['logic'])
        run=WorkerRun.objects.create(batch=batch,worker='fixture',state='running',report={'findings':[{'id':'original'}]})
        self.c.batch=batch;self.c.save();self.send();self.execute()
        batch.refresh_from_db();run.refresh_from_db();self.c.refresh_from_db()
        self.assertEqual(batch.status,'running');self.assertEqual(run.report,{'findings':[{'id':'original'}]});self.assertEqual(self.c.batch,batch)
    def test_history_has_only_own_turns_and_completed_model_answers(self):
        self.send();self.execute()
        Conversation.objects.create(owner=self.other).messages.create(role='user',text='OTHER-SECRET')
        self.c.messages.create(role='assistant',text='CONTROL-STUB',metadata={'kind':'progress'})
        self.key=str(uuid.uuid4());self.send('Прибавь ещё 3')
        messages,_=model.history(self.row() if ChatResponse.objects.count()==1 else ChatResponse.objects.latest('created'))
        contents=[m['content'] for m in messages]
        self.assertIn('2 + 2 = 4.',contents);self.assertIn('Прибавь ещё 3',contents)
        self.assertNotIn('OTHER-SECRET',contents);self.assertNotIn('CONTROL-STUB',contents)
    def test_history_is_bounded_and_never_begins_with_orphan_answer(self):
        for i in range(40):
            self.c.messages.create(role='user',text='question'+str(i))
            self.c.messages.create(role='assistant',text='answer'+str(i),metadata={'kind':'model','state':'done'})
        self.send();messages,omitted=model.history(self.row())
        self.assertTrue(omitted);self.assertLessEqual(len(messages),26);self.assertEqual(messages[1]['role'],'user')
        self.assertEqual(messages[-1]['content'],'сколько будет 2+2')
    def test_exact_context_trim_removes_whole_turns(self):
        for i in range(10):self.c.messages.create(role='user',text='old '+str(i)+'x'*2000)
        self.send();self.execute();self.assertTrue(self.row().assistant_message.metadata['context_omitted'])
        self.assertEqual(self.generation['messages'][1]['role'],'user');self.assertEqual(self.generation['messages'][-1]['content'],'сколько будет 2+2')
    def test_oversize_current_message_fails_without_generation_and_releases_ticket(self):
        self.config.context_tokens=600;self.config.save();self.send('x'*5000)
        with patch.object(model.Gateway,'http',autospec=True,side_effect=self.transport):model.drain()
        self.assertEqual(self.row().state,'failed');self.assertIn('не помещается',self.row().assistant_message.text)
        self.assertNotIn('/v1/chat/completions',self.paths);self.assertEqual(self.paths[-1],'/pipeline/ticket')
    def test_same_key_different_text_is_rejected(self):
        self.send();self.assertEqual(self.send('другой текст').status_code,409)
    def test_cross_owner_history_and_model_actions_are_not_accessible(self):
        self.send();row=self.row();self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code,404)
        self.assertEqual(self.client.post(self.url+'model/',json.dumps({'response':str(row.pk),'action':'cancel'}),content_type='application/json').status_code,404)
    def test_response_from_other_conversation_cannot_be_cancelled(self):
        self.send();row=self.row();other=Conversation.objects.create(owner=self.user)
        response=self.client.post(f'/normcontol/chat/conversations/{other.pk}/model/',json.dumps({'response':str(row.pk),'action':'cancel'}),content_type='application/json')
        self.assertEqual(response.status_code,404);row.refresh_from_db();self.assertEqual(row.state,'queued')
    def test_queue_cancel_and_explicit_retry_preserve_message_identity(self):
        self.send();row=self.row();model.action(self.c,row.pk,'cancel');self.assertIsNone(model.claim())
        model.action(self.c,row.pk,'retry');self.execute();self.assertEqual(self.row().state,'done');self.assertEqual(self.c.messages.count(),2)
    def test_running_cancel_cannot_be_overwritten_by_late_answer(self):
        self.send();pk,lease=model.claim();model.action(self.c,pk,'cancel')
        with self.assertRaises(model.Cancelled):model.update(pk,lease,text='late reply',state='done')
        self.assertEqual(self.row().assistant_message.text,'Запрос отменён.')
    def test_only_one_global_chat_claim_at_a_time(self):
        self.send();other=Conversation.objects.create(owner=self.other);model.enqueue(other,uuid.uuid4(),'fixture')
        self.assertIsNotNone(model.claim());self.assertIsNone(model.claim())
    def test_expired_lease_is_not_replayed_silently(self):
        self.send();model.claim();ChatResponse.objects.update(lease_until=timezone.now()-timedelta(seconds=1))
        model.recover();self.assertEqual(self.row().state,'failed');self.assertIsNone(model.claim())
        self.assertIn('повторить',self.row().assistant_message.text)
    def test_inactive_owner_blocks_generation(self):
        self.send();self.user.is_active=False;self.user.save()
        with patch.object(model.Gateway,'http',autospec=True,side_effect=self.transport):model.drain()
        self.assertEqual(self.row().state,'failed');self.assertFalse(self.paths)
    def test_raw_gateway_error_and_credentials_are_never_shown(self):
        self.send()
        with patch.object(model,'execute',side_effect=OSError('secret-key fixture-url')):model.drain()
        self.assertEqual(self.row().state,'failed');self.assertNotIn('secret-key',self.row().assistant_message.text)
    def test_malformed_reply_is_a_failed_turn(self):
        self.send();original=self.transport
        def bad(gateway,path,value=None,timeout=15):
            if path=='/v1/chat/completions':return {'choices':[{'message':{'content':None}}]}
            return original(gateway,path,value,timeout)
        with patch.object(model.Gateway,'http',autospec=True,side_effect=bad):model.drain()
        self.assertEqual(self.row().state,'failed')
    def test_no_connection_reports_setup_error(self):
        self.send();LLMConfig.objects.all().delete();model.drain();self.assertEqual(self.row().state,'failed')
        self.assertIn('не настроено',self.row().assistant_message.text)
    def test_dialogue_requests_wake_existing_model_startup_and_respect_manual_stop(self):
        self.send();runtime=LLMRuntime.objects.create(pk=1)
        self.assertEqual(pending_demand(),'chat:'+str(self.row().pk));self.assertTrue(queue_start(runtime)['automatic'])
        runtime.command={'action':'stop','state':'done','created':(timezone.now()+timedelta(seconds=1)).isoformat()}
        self.assertEqual(queue_start(runtime),runtime.command)
    def test_draft_mode_and_dialogue_history_survive_reload(self):
        self.client.post(self.url,json.dumps({'text':'Привет','mode':'dialogue'}),content_type='application/json')
        self.assertEqual(self.client.get(self.url).json()['draft']['mode'],'dialogue');self.send()
        self.assertEqual(self.client.get(self.url).json()['messages'][-1]['metadata']['state'],'queued')
    def test_late_autosave_cannot_restore_a_submitted_prompt(self):
        self.send()
        result=self.client.post(self.url,json.dumps({'text':'сколько будет 2+2','message_id':0}),content_type='application/json')
        self.assertEqual(result.status_code,409);self.c.refresh_from_db();self.assertEqual(self.c.draft,{})
        latest=self.c.messages.order_by('-id').first().pk
        result=self.client.post(self.url,json.dumps({'text':'следующий вопрос','message_id':latest}),content_type='application/json')
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['draft']['text'],'следующий вопрос')
