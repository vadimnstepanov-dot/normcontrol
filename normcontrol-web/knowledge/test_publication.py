import copy,json,uuid
from unittest.mock import patch
from django.test import TestCase,override_settings
from .test_expert import ExpertTests
from .test_uploads import TOKEN
from .models import ExpertCard,Release,DocumentProfile,Command
from . import services as s
from .profiles import save_profile
from knowledge_v2.publication import material
from knowledge_v2.review import ledger,aggregate
from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class PublicationTests(TestCase):
    setUp=ExpertTests.setUp;transport=ExpertTests.transport;download=ExpertTests.download
    upload=ExpertTests.upload;prepared=ExpertTests.prepared;ready=ExpertTests.ready
    request=ExpertTests.request;work=ExpertTests.work

    def release(self):
        self.dataset.refresh_from_db();self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        c=s.prepare_selected_sources(self.user,self.dataset.pk,[str(self.card.source_id)],self.dataset.metadata_revision,str(uuid.uuid4()))
        self.bridge.once();c.refresh_from_db();self.assertEqual(c.state,'done',c.result)
        r=Release.objects.get(pk=c.payload['release_id'])
        s.publish(self.user,self.dataset.pk,r.pk,self.dataset.metadata_revision,str(uuid.uuid4()));self.bridge.once()
        self.dataset.refresh_from_db();return r,material(self.store,str(r.pk))

    def facts(self):
        return {'selected_sources':dict(value=[str(self.card.source_id)],complete=True,evidence=[dict(source='test',locator='selection')])}

    def test_unconfirmed_and_confirmed_reports_have_distinct_trust(self):
        self.ready();old,p=self.release()
        rows=ledger(self.store,str(old.pk),list(p['profiles'].values()),self.facts(),lambda *_:True,lambda *_:True)
        self.assertTrue(rows);self.assertTrue(all(x['preliminary_only'] for x in rows))
        row=next(x for x in rows if x['lineage']==str(self.card.pk))
        self.assertEqual(row['execution_issues'],[],row)
        scope=dict(expected_ids=['b'],gaps=[])
        batches=[dict(id='batch',payload=dict(obligations=[row],documents=[dict(id='b')]))]
        result={'batch':{'decisions':[dict(obligation_id=row['id'],outcome='violated',claim='contradiction',reason='Verified conflict',evidence=[])]}}
        d=aggregate([row],batches,result,[],scope)[0]
        self.assertTrue(d['preliminary_violation']);self.assertEqual(d['state'],'unknown')
        self.request('confirm',acknowledge_questions=True);self.work()
        new,p2=self.release();rows2=ledger(self.store,str(new.pk),list(p2['profiles'].values()),self.facts(),lambda *_:True,lambda *_:True)
        row2=next(x for x in rows2 if x['lineage']==str(self.card.pk))
        self.assertFalse(row2['preliminary_only']);self.assertEqual(row2['issues'],[])
        batches[0]['payload']['obligations']=[row2];result['batch']['decisions'][0]['obligation_id']=row2['id']
        self.assertEqual(aggregate([row2],batches,result,[],scope)[0]['state'],'violated')
        self.assertTrue(material(self.store,str(old.pk))['catalog'][0]['trust']['preliminary_only'])
        endpoint=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/releases/{new.pk}/material/'
        self.assertTrue(self.client.get(endpoint+'?kind=diff').json()['entries'])
        self.client.force_login(self.other);self.assertEqual(self.client.get(endpoint).status_code,403)

    def test_diamond_inheritance_no_duplicate_and_parent_change_invalidates(self):
        self.ready();self.user.is_staff=True;self.user.save()
        root=next(p for p in DocumentProfile.objects.filter(scope=self.scope) if any(b['source_id']==str(self.card.source_id) and b['profile_id']==self.card.profile_key for b in p.definition['bindings']))
        def child(name,parents):return save_profile(self.user,self.scope.pk,dict(name=name,description='Test profile',
            expression=root.definition['expression'],parents=[str(x.pk) for x in parents],bindings=[]))
        a=child('Branch A',[root]);b=child('Branch B',[root]);leaf=child('Leaf',[a,b])
        self.request('confirm',acknowledge_questions=True);self.work();old,pm=self.release()
        rows=ledger(self.store,str(old.pk),[pm['profiles'][str(leaf.pk)]],self.facts(),lambda *_:True,lambda *_:True)
        self.assertEqual(len(rows),len({(x['requirement_ref'][0],x['obligation_id']) for x in rows}))
        old_hash=old.manifest_hash;snap=s.create_snapshot(self.user,uuid.uuid4(),[str(self.dataset.pk)],{'engine':'test'})
        changed=copy.deepcopy(root.definition);changed['expression']={'all_of':[changed['expression'],{'fact':{'name':'stage','in':['design']}}]}
        save_profile(self.user,self.scope.pk,changed,root.pk,root.revision)
        new,pm2=self.release();same=next(x for x in pm2['catalog'] if x['lineage']==str(self.card.pk))
        self.assertTrue(same['trust']['requires_reconfirmation'])
        old.refresh_from_db();snap.refresh_from_db();self.assertEqual(old.manifest_hash,old_hash)
        self.assertEqual(snap.data['releases'][0]['manifest_hash'],old_hash)
        from knowledge_v2.curation import diff
        changes=diff(pm['catalog'],pm2['catalog']);self.assertTrue(any('context_changed' in x.get('changes',[]) for x in changes))
        self.assertTrue(any(str(leaf.pk) in x['affected_profiles'] for x in changes))

    def test_publish_refuses_stale_editor_selection(self):
        self.ready();self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        c=s.prepare_selected_sources(self.user,self.dataset.pk,[str(self.card.source_id)],1,'freeze')
        self.bridge.once();rid=c.payload['release_id']
        self.request('edit',patch={'description':'New expert version after preparation.'});self.work()
        with self.assertRaises(s.Conflict):s.publish(self.user,self.dataset.pk,rid,1,'stale')
        self.assertEqual(Release.objects.get(pk=rid).state,'ready')

    def test_changed_related_norm_invalidates_confirmation_and_historical_view_is_read_only(self):
        self.ready();self.request('confirm',acknowledge_questions=True);self.work()
        target=self.card;old,pm=self.release()
        before=next(x for x in pm['catalog'] if x['lineage']==str(target.pk))
        self.assertTrue(before['trust']['approval_current'])
        self.card=ExpertCard.objects.filter(source=target.source,profile_key=target.profile_key).exclude(pk=target.pk).first()
        self.assertIsNotNone(self.card)
        self.request('edit',patch={'description':'Изменённое общее условие из источника.'});self.work()
        new,pm2=self.release();after=next(x for x in pm2['catalog'] if x['lineage']==str(target.pk))
        self.assertEqual(before['description'],after['description']);self.assertTrue(after['trust']['requires_reconfirmation'])
        r=self.client.get(f'/normcontol/api/v2/expert/cards/{target.pk}/?revision=1').json()
        self.assertEqual(r['revision'],1);self.assertFalse(r['can_edit']);self.assertFalse(r['can_refine'])
        self.assertEqual(r['status'],'unreviewed')
        compare=self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/compare/',{'old':old.pk,'new':new.pk})
        self.assertEqual(compare.status_code,200,compare.content);self.assertTrue(compare.json()['semantic'])
        self.assertContains(self.client.get(f'/normcontol/knowledge/releases/{new.pk}/'),'Изменения и влияние')

    def test_local_exception_keeps_parent_and_requires_evidenced_fact(self):
        self.ready();parent=self.card;root=next(p for p in DocumentProfile.objects.filter(scope=self.scope) if any(b['source_id']==str(self.card.source_id) and b['profile_id']==self.card.profile_key for b in p.definition['bindings']))
        child=DocumentProfile.objects.create(scope=self.scope,name='Local',definition=dict(name='Local',description='Local scope',
            expression=root.definition['expression'],parents=[str(root.pk)],bindings=[]))
        r=self.request('refine',profile_id=str(child.pk),refinement_kind='exception',basis_index=0,
            description='Explicit local exception, expert test fixture.',condition={'fact':{'name':'exception_allowed','in':[True]}})
        self.assertEqual(r.status_code,202,r.content);self.work()
        derived=ExpertCard.objects.get(history__action='refine');self.assertEqual(parent.revision,1)
        self.card=derived;self.request('confirm',acknowledge_questions=True);self.work()
        release,pm=self.release();rows=ledger(self.store,str(release.pk),list(pm['profiles'].values()),self.facts(),lambda *_:True,lambda *_:True)
        base=next(x for x in rows if x['lineage']==str(parent.pk))
        self.assertIn('Normative exception condition unknown',base['issues'])
        facts=self.facts();facts['exception_allowed']=dict(value=True,evidence=[dict(source='test',locator='approval')])
        rows=ledger(self.store,str(release.pk),list(pm['profiles'].values()),facts,lambda *_:True,lambda *_:True)
        self.assertEqual(next(x for x in rows if x['lineage']==str(parent.pk))['applicability']['result'],'not_applicable')
