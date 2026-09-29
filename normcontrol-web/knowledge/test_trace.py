import copy,json,uuid
from django.test import TestCase,override_settings
from .test_publication import PublicationTests
from .test_expert import ExpertTests
from .test_uploads import TOKEN
from .models import ExpertCard,NormativeLink,Membership
from . import trace_links,services
from knowledge_v2.store import Conflict

@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class TracePortalTests(TestCase):
    setUp=ExpertTests.setUp;transport=ExpertTests.transport;download=ExpertTests.download
    upload=ExpertTests.upload;prepared=ExpertTests.prepared;ready=ExpertTests.ready
    request=ExpertTests.request;work=ExpertTests.work;release=PublicationTests.release
    def prepare(self):
        self.ready();cards=list(ExpertCard.objects.filter(source=self.card.source).exclude(pk=self.card.pk))
        self.target=cards[0]
        self.value=dict(source=str(self.card.pk),target=str(self.target.pk),basis=str(self.card.pk),
            source_type='ТЗ',target_type='ЧТЗ',relation='preserves',description='Сохранить ограничение исходного требования.',
            condition={'fact':{'name':'document_type','in':['ТЗ']}},mandatory_target=False,confidence=.75,status='draft',
            reason='Точная версия нормы и связанные требования проверены по источнику.')
    def test_link_editor_versions_acl_and_stale_update(self):
        self.prepare();url='/normcontol/api/v2/trace/links/'
        def post(value,path=url):return self.client.post(path,data=json.dumps(value),content_type='application/json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        r=post(dict(self.value,set_id=str(self.dataset.pk)));self.assertEqual(r.status_code,201,r.content)
        row=r.json();path=url+row['id']+'/'
        self.assertEqual(post(dict(self.value,expected_revision=1),path).status_code,200)
        self.assertEqual(post(dict(self.value,expected_revision=1),path).status_code,409)
        self.assertEqual(len(self.client.get(path).json()['history']),2)
        self.assertContains(self.client.get('/normcontol/knowledge/trace/'),'Связи требований')
        self.client.force_login(self.other);self.assertEqual(self.client.get(url+'?set_id='+str(self.dataset.pk)).status_code,403)

    def test_link_basis_extensions_survive_edit_and_revision(self):
        self.prepare()
        value=dict(self.value,basis_card_ids=[self.value['basis']],evidence_contract={'required':['source','target']},normative_basis=[{'card_id':self.value['basis']}])
        row=trace_links.save(self.user,self.dataset.pk,value)
        edited=trace_links.save(self.user,self.dataset.pk,dict(self.value,expected_revision=1),row.pk)
        self.assertEqual(edited.payload['normative_basis'],value['normative_basis'])
        self.assertEqual(edited.payload['evidence_contract'],value['evidence_contract'])
        self.assertEqual(edited.history.last().payload['basis_card_ids'],value['basis_card_ids'])
    def test_release_pins_links_and_changes_require_new_release(self):
        self.prepare();row=trace_links.save(self.user,self.dataset.pk,self.value)
        r,policy=self.release();self.assertEqual(policy['links'][0]['id'],str(row.pk));self.assertFalse(policy['links'][0]['trusted'])
        trace_links.save(self.user,self.dataset.pk,dict(self.value,expected_revision=1,description='Новое смысловое уточнение нормативной связи.'),row.pk)
        from knowledge_v2.publication import material
        self.assertEqual(material(self.store,str(r.pk))['links'][0]['revision'],1)
        new,p2=self.release();self.assertEqual(p2['links'][0]['revision'],2)
    def test_changed_endpoint_cannot_silently_keep_confirmed_link(self):
        self.prepare();trace_links.save(self.user,self.dataset.pk,self.value)
        self.request('edit',patch={'description':'Уточнённое содержание нормы.'});self.work()
        self.dataset.refresh_from_db()
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        c=services.prepare_selected_sources(self.user,self.dataset.pk,[str(self.card.source_id)],self.dataset.metadata_revision,str(uuid.uuid4()))
        with self.assertRaises(Conflict):self.bridge.once()
        c.refresh_from_db();self.assertEqual(c.state,'failed')
    def test_contributor_cannot_confirm_link(self):
        self.prepare();Membership.objects.create(scope=self.scope,user=self.other,role='contributor')
        self.client.force_login(self.other)
        d=dict(self.value,set_id=str(self.dataset.pk),status='confirmed')
        self.assertEqual(self.client.post('/normcontol/api/v2/trace/links/',data=json.dumps(d),content_type='application/json').status_code,403)
    def test_new_trace_check_contract_cannot_be_claimed_by_old_worker(self):
        self.prepare();trace_links.save(self.user,self.dataset.pk,self.value)
        c=services.prepare_selected_sources(self.user,self.dataset.pk,[str(self.card.source_id)],1,'new-engine')
        self.assertIsNone(services.claim('previous-worker',['release.prepare']))
        claimed=services.claim('current-worker',['release.prepare','trace.suggest'])
        self.assertEqual(claimed['command_id'],str(c.pk))

    def test_repeated_editor_request_does_not_create_another_revision(self):
        self.prepare();key=str(uuid.uuid4())
        first=trace_links.save(self.user,self.dataset.pk,self.value,key=key)
        repeated=trace_links.save(self.user,self.dataset.pk,self.value,key=key)
        self.assertEqual(str(first.pk),str(repeated.pk));self.assertEqual(repeated.revision,1)
        self.assertEqual(first.history.count(),1)
        with self.assertRaises(services.Conflict):trace_links.save(self.user,self.dataset.pk,dict(self.value,description='Иное изменение.'),key=key)

    def test_link_diff_is_visible_without_mutating_old_release(self):
        self.prepare();row=trace_links.save(self.user,self.dataset.pk,self.value);old,_=self.release()
        trace_links.save(self.user,self.dataset.pk,dict(self.value,expected_revision=1,description='Уточнённая связь с нормативным основанием.'),row.pk)
        new,_=self.release()
        response=self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/compare/?old={old.pk}&new={new.pk}')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['trace_links']['count'],1)
        self.assertEqual(response.json()['trace_links']['examples'][0]['kind'],'changed')
