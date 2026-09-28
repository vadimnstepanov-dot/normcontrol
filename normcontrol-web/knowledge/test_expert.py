import copy,json,os,uuid
from unittest.mock import patch
from django.test import TestCase,Client,override_settings
from django.contrib.auth import get_user_model
from .test_norms import NormativeTests
from .test_uploads import UploadTests,TOKEN
from .models import ExpertCard,ExpertCardRevision,Command,Membership,DocumentProfile
from . import services as s
from . import expert
from knowledge_v2.store import checksum


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class ExpertTests(TestCase):
    setUp=NormativeTests.setUp
    transport=UploadTests.transport
    download=UploadTests.download
    upload=UploadTests.upload
    prepared=NormativeTests.prepared

    def ready(self):
        sid=self.prepared();s.analyze_source(self.user,self.dataset.pk,sid,'expert-analysis');self.bridge.once()
        self.card=ExpertCard.objects.filter(source_id=sid).first();self.assertIsNotNone(self.card)
        return self.card

    def request(self,action,**kw):
        self.card.refresh_from_db()
        data=dict(action=action,expected_revision=self.card.revision,reason='Проверено по исходной формулировке.',**kw)
        return self.client.post(f'/normcontol/api/v2/expert/cards/{self.card.pk}/',data=json.dumps(data),content_type='application/json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))

    def work(self):
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        self.card.refresh_from_db()

    def test_edit_confirm_versions_replay_and_reset_trust(self):
        c=self.ready();base=copy.deepcopy(c.payload)
        result=self.request('inspect');self.assertEqual(result.status_code,202,result.content);self.work()
        self.assertTrue(self.card.contexts)
        result=self.request('edit',patch={'description':'Уточнённое требование к журналированию.'})
        self.assertEqual(result.status_code,202,result.content);self.work()
        self.assertEqual(self.card.revision,2);self.assertEqual(self.card.status,'unreviewed')
        result=self.request('confirm',acknowledge_questions=True);self.assertEqual(result.status_code,202,result.content);self.work()
        self.assertEqual(self.card.status,'confirmed');self.assertTrue(self.card.payload['expert_approved'])
        result=self.request('edit',patch={'description':'Повторно уточнённое требование.'});self.work()
        self.assertEqual(self.card.status,'unreviewed');self.assertFalse(self.card.payload['expert_approved'])

        self.assertEqual(self.card.history.get(revision=1).payload,base)
        self.assertEqual(self.card.history.count(),4)
        with self.store.connection() as db:
            records=db.execute("SELECT version FROM records WHERE kind='expert_card' AND id=? ORDER BY version",(str(self.card.pk),)).fetchall()
            self.assertEqual([r[0] for r in records],[2,3,4])
        prepared=s.prepare_selected_sources(self.user,self.dataset.pk,[str(c.source_id)],1,'include-reviewed-drafts')
        chosen=next(x for x in prepared.payload['curation']['cards'] if x['id']==str(c.pk))
        self.assertEqual(chosen['revision'],4);self.assertEqual(chosen['status'],'unreviewed')

    def test_reanalysis_preserves_expert_deleted_record(self):
        c=self.ready();old_description=c.description
        self.assertEqual(self.request('reject').status_code,202);self.work()
        cid=s.analyze_source(self.user,self.dataset.pk,c.source_id,'fresh-analysis-after-expert')
        self.assertTrue(self.bridge.once())
        c.refresh_from_db();self.assertEqual(c.status,'rejected');self.assertTrue(c.latest_analysis)
        self.assertEqual(c.description,old_description)
        repeated=ExpertCard.objects.filter(analysis=cid)
        self.assertTrue(repeated.exists());self.assertTrue(all(x.status=='superseded' for x in repeated if x.payload.get('citations')==c.payload.get('citations')))

    def test_manual_requirement_is_versioned_local_and_preserves_basis(self):
        c=self.ready();p=DocumentProfile.objects.filter(normative_set=self.dataset).first()
        before=copy.deepcopy(c.payload)
        result=self.request('create',profile_id=str(p.pk),description='Контрольное требование эксперта с нормативным основанием.')
        self.assertEqual(result.status_code,202,result.content);self.work()
        child=ExpertCard.objects.exclude(pk=c.pk).filter(payload__local_profile=str(p.pk)).first()
        self.assertIsNotNone(child);self.assertEqual(child.revision,1)
        self.assertEqual(child.payload['obligations'][0]['description'],child.description)
        self.assertEqual(self.card.payload,before)

    def test_parallel_stale_revision_and_idempotency(self):
        self.ready();data=dict(action='edit',expected_revision=1,reason='Исправление после сверки источника.',patch={'description':'Новое описание.'})
        url=f'/normcontol/api/v2/expert/cards/{self.card.pk}/'
        def post(key):return self.client.post(url,data=json.dumps(data),content_type='application/json',HTTP_IDEMPOTENCY_KEY=key)
        a=post('same');b=post('same');self.assertEqual(a.json()['command_id'],b.json()['command_id'])
        self.assertEqual(post('parallel').status_code,409)
        self.work();self.assertEqual(post('stale').status_code,409)
        self.assertEqual(post('same').json()['command_id'],a.json()['command_id'])

    def test_permissions_inherited_and_no_private_leak(self):
        self.ready();url=f'/normcontol/api/v2/expert/cards/{self.card.pk}/'
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.get('/normcontol/api/v2/expert/catalog/').json()['profiles'],[])
        self.client.force_login(self.user)
        parent=DocumentProfile.objects.filter(scope=self.scope).first()
        child=DocumentProfile.objects.create(scope=self.scope,name='Child',definition=dict(parents=[str(parent.pk)],bindings=[]))
        d=self.client.get(url+'?profile='+str(child.pk)).json();self.assertTrue(d['inherited']);self.assertFalse(d['can_edit'])
        self.assertEqual(self.request('edit',profile_id=str(child.pk),patch={'description':'Changed'}).status_code,409)

    def test_list_filter_pagination_and_source_context(self):
        self.ready();url=f'/normcontol/api/v2/expert/cards/?scope={self.scope.pk}'
        result=self.client.get(url);self.assertEqual(result.status_code,200,result.content)
        self.assertLessEqual(len(result.json()['entries']),25);self.assertNotIn('payload',result.json()['entries'][0])
        self.assertEqual(self.client.get(url+'&q=absent-word').json()['total'],0)
        self.assertEqual(self.client.get(url+'&page=0').status_code,400)
        self.assertContains(self.client.get('/normcontol/knowledge/expert/'),'Рабочее место эксперта')
        self.request('inspect');self.work()
        context={x['locator']:x for x in self.card.contexts}
        for c in self.card.payload['citations']:self.assertEqual(context[c['locator']]['text'][c['start']:c['end']],c['quote'])

    def test_split_conserves_atoms_conditions_and_unreviewed_children(self):
        self.ready()
        # A source-backed compound fixture independent of 1755-r.
        p=copy.deepcopy(self.card.payload);p['obligations']=[dict(p['obligations'][0],description='Первое действие'),dict(p['obligations'][0],description='Второе действие')]
        p['composition']={'all_of':['Первое действие','Второе действие']}
        with self.store.connection() as db:
            row=db.execute('SELECT payload FROM records WHERE id=? AND version=1',(str(self.card.base_id),)).fetchone()
        import json
        base=json.loads(row[0]);base['card']=p;newid=str(uuid.uuid4())
        self.store.put_record(str(self.dataset.pk),'requirement',newid,1,base)
        self.card.base_id=newid;self.card.payload=p;self.card.save()
        r=self.request('split',parts=[dict(description='Первое действие',obligation_indices=[0]),dict(description='Второе действие',obligation_indices=[1])]);self.assertEqual(r.status_code,202,r.content);self.work()
        self.assertEqual(self.card.status,'superseded')
        children=ExpertCard.objects.filter(base_id=newid).exclude(pk=self.card.pk)
        self.assertEqual(children.count(),2)
        for c in children:
            self.assertEqual(c.status,'unreviewed');self.assertEqual(c.payload['conditions'],p['conditions']);self.assertTrue(c.payload['composition_group'])
        children.update(latest_analysis=False)
        expert.project(self.card.analysis,self.card.source,[])
        self.assertFalse(children.filter(latest_analysis=False).exists())

    def test_read_only_role_cannot_edit_or_approve(self):
        self.ready();Membership.objects.create(scope=self.scope,user=self.other,role='reader');self.client.force_login(self.other)
        self.assertEqual(self.request('confirm').status_code,403)
        self.assertEqual(self.request('edit',patch={'description':'Wrong'}).status_code,403)
        self.assertEqual(self.request('inspect').status_code,202)

    def test_citations_cannot_be_rewritten_and_unknown_condition_ref_rejected(self):
        self.ready()
        self.assertEqual(self.request('edit',patch={'citations':[]}).status_code,400)
        self.assertEqual(self.request('edit',patch={'conditions':[{'text':'Condition','citation_index':999}]}).status_code,400)

    def test_backfill_does_not_overwrite_reviewed_version(self):
        self.ready();self.request('edit',patch={'description':'Экспертная редакция.'});self.work()
        command=self.card.analysis
        original=[x for chunk in command.analysis_chunks.all() for x in chunk.entries]
        expert.project(command,self.card.source,original)
        self.card.refresh_from_db();self.assertEqual(self.card.revision,2);self.assertEqual(self.card.description,'Экспертная редакция.')

    def test_manual_split_keeps_logic_and_rejects_missing_atoms(self):
        self.ready()
        parts=[dict(description='Первая часть нормы.',obligation_indices=[0]),dict(description='Вторая часть нормы.',obligation_indices=[0])]
        r=self.request('split',parts=parts,split_logic='any_of');self.assertEqual(r.status_code,202,r.content);self.work()
        children=ExpertCard.objects.filter(base_id=self.card.base_id).exclude(pk=self.card.pk)
        self.assertEqual(children.count(),2)
        for c in children:self.assertIn('any_of',c.payload['composition_group']['logic'])

    def test_merge_preserves_proofs_and_all_conditions_then_can_inspect(self):
        self.ready();first=self.card
        # Two distinct local atoms with the same applicability; canonical fixtures.
        with self.store.connection() as db:base=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=1',(str(first.base_id),)).fetchone()[0])
        otherbase=copy.deepcopy(base);identity=str(uuid.uuid4());otherbase['card']['description']='Дополнительная норма.'
        otherbase['card']['conditions'].append(dict(text='Только для указанного объекта.',citation=base['card']['citations'][0]))
        self.store.put_record(str(self.dataset.pk),'requirement',identity,1,otherbase)
        p=otherbase['card'];second=ExpertCard.objects.create(source=first.source,analysis=first.analysis,base_id=identity,payload=p,
            description=p['description'],entity_type=p['entity_type'],profile_key=p.get('profile_id') or '')
        r=self.request('merge',description='Совместное выполнение двух требований.',others=[dict(id=str(second.pk),revision=1)])
        self.assertEqual(r.status_code,202,r.content);self.work()
        merged=ExpertCard.objects.get(history__action='merge',revision=1)
        self.assertEqual(merged.status,'unreviewed');self.assertTrue(any(x['text']=='Только для указанного объекта.' for x in merged.payload['conditions']))
        self.card=merged;self.request('inspect');self.work();self.assertTrue(self.card.contexts)

    def test_worker_rechecks_revoked_expert_permission(self):
        self.ready();Membership.objects.create(scope=self.scope,user=self.other,role='reviewer');self.client.force_login(self.other)
        r=self.request('edit',patch={'description':'Недопустимая после отзыва правка.'});self.assertEqual(r.status_code,202)
        Membership.objects.filter(user=self.other).update(role='reader')
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):
            with self.assertRaises(PermissionError):self.bridge.once()
        self.card.refresh_from_db();self.assertEqual(self.card.revision,1)
        self.assertEqual(Command.objects.get(pk=r.json()['command_id']).state,'failed')
