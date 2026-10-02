import copy,json,uuid
from unittest.mock import patch
from django.test import TestCase,override_settings
from knowledge.models import Scope,Membership,NormativeSet,Release,SourceUpload,Command,ObjectControl
from knowledge import services
from .models import ChatResponse
from . import chat_model,chat_knowledge
from . import test_chat_model as fixtures

@override_settings(CHAT_MODEL_BACKGROUND=False,KNOWLEDGE_V2_ENABLED=True,CHAT_RAG_TIMEOUT_SECONDS=1)
class DialogueKnowledgeTests(TestCase):
    setUp=fixtures.DialogueTests.setUp
    send=fixtures.DialogueTests.send
    row=fixtures.DialogueTests.row
    transport=fixtures.DialogueTests.transport
    execute=fixtures.DialogueTests.execute

    def area(self,name='СТО РЖД',purpose='normative',owner=None):
        scope=Scope.objects.create(owner=owner or self.user,name=name,kind='project')
        area=NormativeSet.objects.create(scope=scope,name=name,purpose=purpose,created_by=scope.owner,state='ready')
        source=SourceUpload.objects.create(normative_set=area,actor=scope.owner,filename='СТО 1.docx',sha256=uuid.uuid4().hex*2,size=10,storage_key='fixture')
        release=Release.objects.create(id=uuid.uuid4(),normative_set=area,manifest={'items':[{'id':str(source.pk),'kind':'source_revision'}]},manifest_hash='a'*64,attestation={},state='active')
        area.active_release=release;area.save()
        return area,source
    def result(self,area,source):
        return dict(release_id=str(area.active_release_id),entries=[dict(record_id='fixture-record',version=1,release_id=str(area.active_release_id),
            source_id=str(source.pk),quote='ЧТЗ должно раскрывать требования утверждённого ТЗ.',locator='4.2',context_complete=True,
            material_type='requirement',quality={'status':'candidate'},trust={'preliminary_only':True})])
    def finish_search(self,mapping):
        original=services.command
        def finish(user,area,kind,key,payload):
            c=original(user,area,kind,key,payload)
            c.state='done';c.result=mapping[str(area.pk)];c.save();return c
        return patch('knowledge.services.command',side_effect=finish)
    def retrieve(self,mapping):
        with self.finish_search(mapping):return chat_knowledge.retrieve(self.row(),lambda *_:None)
    def test_acl_active_release_and_lifecycle_filter_catalog_before_search(self):
        area,source=self.area();private,_=self.area('PRIVATE',owner=self.other)
        disabled,_=self.area('DISABLED');ObjectControl.objects.create(normative_set=disabled,kind='area',object_id=disabled.pk,enabled=False)
        editing,_=self.area('EDITING');editing.state='editing';editing.save()
        self.send('Что знаешь про СТО?');data=self.retrieve({str(area.pk):self.result(area,source)})
        self.assertEqual([x['name'] for x in data['catalog']],['СТО РЖД'])
        self.assertEqual(Command.objects.count(),1);self.assertNotIn('PRIVATE',json.dumps(data))
    def test_all_accessible_areas_and_experience_are_searched_without_gpu(self):
        a,sa=self.area();b,sb=self.area('Рецензии','experience')
        self.send('Как раскрыть требования ТЗ?');data=self.retrieve({str(a.pk):self.result(a,sa),str(b.pk):self.result(b,sb)})
        self.assertEqual(Command.objects.count(),2);self.assertEqual(len(data['pins']),2)
        self.assertEqual({x['purpose'] for x in data['catalog']},{'normative','experience'})
        self.assertEqual(data['sources'][0]['quality']['status'],'candidate')
        self.assertEqual(data['sources'][0]['label'],'S1')
    def test_followup_search_only_uses_own_questions_and_caps_query(self):
        a,s=self.area();self.c.messages.create(role='user',text='Требования ЧТЗ')
        self.c.messages.create(role='assistant',text='Не передавать модельный ответ в поиск',metadata={'kind':'model','state':'done'})
        self.send('Уточни '+('x'*1400));self.retrieve({str(a.pk):self.result(a,s)})
        query=Command.objects.get().payload['query'];self.assertLessEqual(len(query),1200)
        self.assertIn('Требования ЧТЗ',query);self.assertNotIn('модельный ответ',query)
    def test_failed_search_is_explicit_and_does_not_invent_evidence(self):
        a,s=self.area();self.send('Что в базе?')
        original=services.command
        def failed(user,area,kind,key,payload):
            c=original(user,area,kind,key,payload);c.state='failed';c.save();return c
        with patch('knowledge.services.command',side_effect=failed):data=chat_knowledge.retrieve(self.row(),lambda *_:None)
        self.assertEqual(data['state'],'unavailable');self.assertEqual(data['sources'],[])
        self.assertIn('недоступен',data['limitations'][0])
    def test_empty_search_does_not_claim_absence_of_standard(self):
        a,s=self.area();self.send('Что в базе?');data=self.retrieve({str(a.pk):{'release_id':str(a.active_release_id),'entries':[]}})
        self.assertEqual(data['state'],'available');self.assertTrue(data['catalog']);self.assertFalse(data['sources'])
        self.assertIn('не доказывает',chat_knowledge.RULES)
    def test_old_worker_cannot_take_new_dialogue_search(self):
        a,s=self.area();self.send();self.retrieve({str(a.pk):self.result(a,s)})
        Command.objects.update(state='pending')
        self.assertIsNone(services.claim('legacy',['normative.search']))
        claim=services.claim('rag',['normative.search'],['dialogue-rag-v1']);self.assertIsNotNone(claim)
    @override_settings(KNOWLEDGE_WORKER_TOKEN='fixture-dialogue-token-'+('x'*32))
    def test_http_worker_claim_accepts_rag_feature_and_blocks_unrecognized_feature(self):
        a,s=self.area();self.send();self.retrieve({str(a.pk):self.result(a,s)});Command.objects.update(state='pending')
        headers={'HTTP_AUTHORIZATION':'Bearer fixture-dialogue-token-'+('x'*32)}
        value={'protocol_version':2,'capabilities':['normative.search'],'features':['dialogue-rag-v1']}
        url='/normcontol/api/v2/worker/claim/'
        reply=self.client.post(url,json.dumps(value),content_type='application/json',**headers)
        self.assertEqual(reply.status_code,200,reply.content);self.assertIsNotNone(reply.json()['command'])
        value['features']=['unrecognized-feature']
        self.assertEqual(self.client.post(url,json.dumps(value),content_type='application/json',**headers).status_code,400)
    def test_revoked_access_and_replaced_release_block_saved_search_context(self):
        a,s=self.area(owner=self.other);grant=Membership.objects.create(scope=a.scope,user=self.user,role='reader')
        self.send();data=self.retrieve({str(a.pk):self.result(a,s)})
        grant.delete()
        with self.assertRaisesMessage(ValueError,'Доступ'):chat_knowledge.validate(self.user,data)
        Membership.objects.create(scope=a.scope,user=self.user,role='reader');a.active_release.manifest_hash='b'*64;a.active_release.save()
        with self.assertRaisesMessage(ValueError,'версия'):chat_knowledge.validate(self.user,data)
    def test_model_receives_rag_and_trusted_links_without_creating_review(self):
        a,s=self.area();self.send('Что знаешь про СТО?');original=self.transport
        def response(gateway,path,value=None,timeout=15):
            if path=='/v1/chat/completions':
                self.generation=value
                return {'choices':[{'message':{'content':'Это предварительное требование [S1].'},'finish_reason':'stop'}]}
            return original(gateway,path,value,timeout)
        with self.finish_search({str(a.pk):self.result(a,s)}),patch.object(chat_model.Gateway,'http',autospec=True,side_effect=response):chat_model.execute(*chat_model.claim())
        prompt=json.dumps(self.generation['messages'],ensure_ascii=False)
        self.assertIn('раскрывать требования',prompt);self.assertIn('candidate',prompt);self.assertIn('preliminary_only',prompt)
        self.assertEqual(self.row().state,'done');self.assertIn('Источники из RAG',self.row().assistant_message.text)
        self.assertEqual(self.row().assistant_message.metadata['rag_sources'][0]['record_id'],'fixture-record')
        self.assertFalse(self.c.batch_id)
    def test_context_trimming_reduces_rag_instead_of_dropping_current_question(self):
        a,s=self.area();self.config.context_tokens=2200;self.config.save();self.send('Что такое ЧТЗ?')
        result=self.result(a,s);result['entries'][0]['quote']='x'*2400;result['entries'][0]['context']=[{'exact_text':'y'*1200}]*3
        with self.finish_search({str(a.pk):result}),patch.object(chat_model.Gateway,'http',autospec=True,side_effect=self.transport):chat_model.execute(*chat_model.claim())
        self.assertIn('Что такое ЧТЗ?',self.generation['messages'][-1]['content'])
        self.assertTrue(self.row().assistant_message.metadata['rag_trimmed'])
    def test_revoke_after_generation_never_delivers_knowledge_answer(self):
        a,s=self.area(owner=self.other);grant=Membership.objects.create(scope=a.scope,user=self.user,role='reader');self.send()
        original=self.transport
        def revoke(gateway,path,value=None,timeout=15):
            answer=original(gateway,path,value,timeout)
            if path=='/v1/chat/completions':grant.delete()
            return answer
        with self.finish_search({str(a.pk):self.result(a,s)}),patch.object(chat_model.Gateway,'http',autospec=True,side_effect=revoke):chat_model.drain()
        self.assertEqual(self.row().state,'failed');self.assertNotIn('2 + 2 = 4',self.row().assistant_message.text)
    def test_made_up_citation_is_not_given_trusted_url(self):
        a,s=self.area();self.send();data=self.retrieve({str(a.pk):self.result(a,s)})
        answer=chat_knowledge.citations('Ответ [S999].',data)
        self.assertIn('не подтверждён',answer);self.assertNotIn('https://',answer)
    def test_cited_source_stays_on_current_portal(self):
        a,s=self.area();self.send();data=self.retrieve({str(a.pk):self.result(a,s)})
        answer=chat_knowledge.citations('Ответ [S1].',data)
        self.assertIn(data['sources'][0]['url'],answer)
        self.assertNotIn('https://',answer)
