import json,uuid
from django.test import TestCase,override_settings
from .test_expert import ExpertTests
from .test_uploads import TOKEN
from .models import Command,Release
from . import services as s
from knowledge_v2.tests.test_semantic import Model
from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
from knowledge_v2.store import checksum
from knowledge_v2.quality_audit import audit
from knowledge_v2.publication import material
from knowledge_v2.review import ledger


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class QualityPublicationTests(TestCase):
    setUp=ExpertTests.setUp;transport=ExpertTests.transport;download=ExpertTests.download
    upload=ExpertTests.upload;prepared=ExpertTests.prepared

    def fixture(self,uncertain=False):
        class Extractor(Model):
            signature='quality-test-extractor'
            def complete(self,*args):
                r=super().complete(*args)
                for e in r['value'].get('entities',[]):e['confidence']=.7 if uncertain and e['description']=='24 часа' else .95
                return r
        self.bridge.analysis_client=Extractor();sid=self.prepared()
        c=s.analyze_source(self.user,self.dataset.pk,sid,'quality-analysis');self.bridge.once();c.refresh_from_db()
        run=c.result['summary']['run_id'];p=self.store.directory/'analyses'/(run+'.json');a=json.loads(p.read_text(encoding='utf8'))
        a['complete']=False;a['coverage_audit']['gaps'].append({'locator':'unread-scope','reason':'uncertain'})
        a['summary']['semantic_completeness']='partial';a['summary']['coverage_audit']=a['coverage_audit']
        p.write_text(json.dumps(a,ensure_ascii=False),encoding='utf8');c.result['summary']=a['summary'];c.save(update_fields=['result'])
        class Auditor:
            signature='controlled-quality'
            def complete(self,policy,data,schema):
                return dict(value={'decisions':[dict(id=c['id'],status='ready',reason='controlled test',
                    evidence=[{'locator':data['context'][0]['locator'],'quote':data['context'][0]['text']}]) for c in data['cards']]},seconds=0,usage={})
        audit(self.store,a,Auditor());self.bridge.normative_index=(FakeEncoder(),FakeVectors());return sid,c

    def test_partial_source_requires_explicit_mode_and_remains_preliminary(self):
        sid,c=self.fixture()
        with self.assertRaises(s.NotReady):s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'strict')
        prepare=s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'screen',mode='screened_test')
        self.assertTrue(self.bridge.once());prepare.refresh_from_db();self.assertEqual(prepare.state,'done',prepare.result)
        q=prepare.result['quality_summary'];self.assertFalse(q['complete']);self.assertEqual(q['coverage_gap_count'],1)
        rid=prepare.payload['release_id'];s.publish(self.user,self.dataset.pk,rid,1,'publish-screen');self.bridge.once()
        policy=material(self.store,rid);facts={'selected_sources':dict(value=[sid],complete=True,evidence=[{'source':'doc'}])}
        rows=ledger(self.store,rid,list(policy['profiles'].values()),facts,lambda *_:True,lambda *_:True)
        self.assertTrue(rows);self.assertTrue(all(r['preliminary_only'] for r in rows))
        approved=[x for x in policy['catalog'] if x['trust']['approval_current']];self.assertEqual(approved,[])
        endpoint=f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/releases/{rid}/material/'
        ready=self.client.get(endpoint+'?quality=ready').json()
        self.assertEqual(ready['total'],q['counts']['ready'])
        profile=policy['profile_snapshot'][0]['id']
        filtered=self.client.get(endpoint+'?quality=ready&profile='+profile).json()
        expected=[r for r in policy['catalog'] if r['quality']['status']=='ready' and profile in r['profiles']]
        self.assertEqual(filtered['total'],len(expected))
        self.assertEqual(self.client.get(endpoint+'?profile=missing-profile').json()['total'],0)
        self.client.force_login(self.other);self.assertEqual(self.client.get(endpoint).status_code,403)

    def test_changed_analysis_cannot_be_used_after_selection(self):
        sid,c=self.fixture();p=s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'freeze',mode='screened_test')
        path=self.store.directory/'analyses'/(c.result['summary']['run_id']+'.json')
        a=json.loads(path.read_text(encoding='utf8'));a['summary']['analysis_errors']=1;path.write_text(json.dumps(a),encoding='utf8')
        with self.assertRaises(Exception):self.bridge.once()
        self.assertFalse(Release.objects.filter(pk=p.payload['release_id']).exists())

    def test_uncertain_card_is_visible_to_expert_but_absent_from_execution_and_search(self):
        from knowledge_v2.search import HybridSearch
        sid,_=self.fixture(uncertain=True)
        command=s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'candidate-selection',mode='screened_test')
        self.bridge.once();command.refresh_from_db();self.assertEqual(command.state,'done')
        rid=command.payload['release_id'];s.publish(self.user,self.dataset.pk,rid,1,'publish-candidates');self.bridge.once()
        policy=material(self.store,rid)
        candidates={tuple(r['ref']) for r in policy['catalog'] if r['quality']['status']=='candidate'}
        self.assertTrue(candidates)
        facts={'selected_sources':dict(value=[sid],complete=True,evidence=[{'source':'doc'}])}
        rows=ledger(self.store,rid,list(policy['profiles'].values()),facts,lambda *_:True,lambda *_:True)
        self.assertTrue(rows)
        self.assertFalse(candidates & {tuple(r['requirement_ref']) for r in rows})
        search=HybridSearch(self.store,*self.bridge.normative_index,lambda *_:True)
        results=search.reference(rid,'24 часа',kinds=('requirement',))
        self.assertFalse(candidates & {(r['record_id'],r['version']) for r in results})
        response=self.client.get(f'/normcontol/api/v2/normative-sets/{self.dataset.pk}/releases/{rid}/material/?quality=candidate')
        self.assertEqual(response.json()['total'],len(candidates))
        from portal.models import Batch
        batch=Batch.objects.create(owner=self.user,name='Quality selection',status='prepared',checks=['sto'])
        page=self.client.get(f'/normcontol/knowledge/check/{batch.pk}/')
        self.assertEqual(page.status_code,200)
        selected=next(n for n in page.context['norms'] if n['id']==str(self.dataset.pk))
        ready=sum(r['quality']['status']=='ready' for r in policy['catalog'])
        self.assertEqual(selected['requirements'],ready)
        self.assertEqual(selected['candidates'],len(candidates))
        self.assertContains(page,'охват СТО неполный')
        self.assertContains(page,'в проверку не включены')

    def test_retry_cannot_change_release_mode(self):
        sid,c=self.fixture();s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'same',mode='screened_test')
        with self.assertRaises(s.Conflict):s.prepare_selected_sources(self.user,self.dataset.pk,[sid],1,'same')

    def test_large_manifest_limit_is_worker_only_and_authentication_precedes_body(self):
        from django.test import RequestFactory
        from django.http import JsonResponse
        from .views import boundary
        factory=RequestFactory();body=json.dumps({'manifest':'x'*(3*1024*1024)})
        def result(request):return JsonResponse({'accepted':True})
        request=factory.post('/',body,content_type='application/json',HTTP_AUTHORIZATION='Bearer '+TOKEN)
        self.assertEqual(boundary({'POST'},worker=True,max_body=16*1024*1024)(result)(request).status_code,200)
        self.assertEqual(boundary({'POST'},worker=True)(result)(request).status_code,413)
        request=factory.post('/',body,content_type='application/json')
        self.assertEqual(boundary({'POST'},worker=True,max_body=16*1024*1024)(result)(request).status_code,403)
