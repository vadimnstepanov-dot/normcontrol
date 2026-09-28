import copy,json,uuid,os
from unittest.mock import patch
from django.test import TestCase,override_settings
from .test_expert import ExpertTests
from .test_uploads import TOKEN
from .models import ExpertCard,Command
from knowledge_v2.portable_area import validate


@override_settings(KNOWLEDGE_WORKER_TOKEN=TOKEN,KNOWLEDGE_WORKER_ID='test-worker')
class PortableTests(TestCase):
    setUp=ExpertTests.setUp
    def transport(self,path,data):
        if path=='/worker/events/':
            from .services import accept_event
            return accept_event('test-worker',**data)
        return ExpertTests.transport(self,path,data)
    download=ExpertTests.download
    upload=ExpertTests.upload
    prepared=ExpertTests.prepared
    ready=ExpertTests.ready

    def export(self):
        self.ready()
        r=self.client.get(f'/normcontol/api/v2/areas/{self.dataset.pk}/export.json')
        self.assertEqual(r.status_code,200,r.content)
        return r.json()

    def post(self,doc,apply=False,key='portable-test',**more):
        self.dataset.refresh_from_db()
        return self.client.post(f'/normcontol/api/v2/areas/{self.dataset.pk}/import/',json.dumps({**dict(document=doc,apply=apply,expected_revision=self.dataset.metadata_revision),**more}),content_type='application/json',HTTP_IDEMPOTENCY_KEY=key)

    def test_roundtrip_proof_trust_idempotency_and_history(self):
        doc=self.export()
        doc['controls']=[dict(kind='card',id=doc['requirements'][0]['id'],enabled=False,deleted=False)]
        validate(doc)
        doc['requirements'][0]['payload']['expert_approved']=True
        doc['requirements'][0]['payload']['expert_approval']={'forged':True}
        self.assertEqual(self.post(doc).status_code,200)
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        original=self.bridge.transport
        def transport(path,data):
            if path=='/worker/events/':
                from .services import accept_event
                return accept_event('test-worker',**data)
            return original(path,data)
        self.bridge.transport=transport
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        c=Command.objects.get(pk=r.json()['command_id']);self.assertEqual(c.state,'done',c.result)
        imported=ExpertCard.objects.filter(status='unreviewed').exclude(pk=self.card.pk).first()
        self.assertIsNotNone(imported)
        self.assertFalse(imported.payload['expert_approved']);self.assertNotIn('expert_approval',imported.payload)
        self.card.refresh_from_db();self.assertEqual(self.card.status,'superseded')
        self.assertTrue(imported.history.exists())
        from .models import ObjectControl
        self.assertTrue(ObjectControl.objects.filter(normative_set=self.dataset,kind='card',enabled=False).exists())
        self.assertEqual(self.post(doc,True).json()['command_id'],str(c.pk))
        self.assertEqual(Command.objects.filter(kind='area.import').count(),1)

    def test_missing_original_and_external_quote_rejected(self):
        doc=self.export();bad=copy.deepcopy(doc);bad['sources'][0]['sha256']='f'*64
        r=self.post(bad);self.assertEqual(r.status_code,409);self.assertEqual(r.json()['missing_sources'][0]['sha256'],'f'*64)
        bad=copy.deepcopy(doc);bad['requirements'][0]['payload']['citations'][0]['quote']='Invented normative text'
        self.assertEqual(self.post(bad).status_code,400)
        self.assertFalse(Command.objects.filter(kind='area.import').exists())

    def test_acl_cycles_and_stale_confirmation(self):
        doc=self.export();self.client.force_login(self.other)
        self.assertEqual(self.client.get(f'/normcontol/api/v2/areas/{self.dataset.pk}/export.json').status_code,403)
        self.assertEqual(self.post(doc).status_code,403)
        self.client.force_login(self.user)
        self.assertEqual(self.post(doc,True,expected_revision=999).status_code,409)
        bad=copy.deepcopy(doc);p=bad['profiles'][0];p['definition']['parents']=[p['id']]
        self.assertEqual(self.post(bad).status_code,400)

    def test_nested_citation_checked_by_canonical_and_no_partial_projection(self):
        doc=self.export();card=doc['requirements'][0]['payload']
        forged=dict(card['citations'][0],quote='Absent condition')
        card['conditions']=[dict(text='Absent condition',citation=forged)]
        r=self.post(doc,True);self.assertEqual(r.status_code,202)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):
            with self.assertRaises(ValueError):self.bridge.once()
        self.card.refresh_from_db();self.assertEqual(self.card.status,'unreviewed')
        self.assertEqual(ExpertCard.objects.count(),len(doc['requirements']))

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        self.ready()
        url=f'/normcontol/api/v2/areas/{self.dataset.pk}/import/'
        for body in ('{"document":{},"document":{},"apply":false}','{"document":NaN,"apply":false}'):
            self.assertEqual(self.client.post(url,body,content_type='application/json').status_code,400)

    def test_transfer_to_new_area_remaps_source_and_profile_ids(self):
        from . import services
        from .models import DocumentProfile
        from knowledge_v2.tests.test_ingest import docx
        from pathlib import Path
        doc=self.export();old_area=self.dataset
        # Reuse the exact original, not a freshly generated ZIP with different timestamps.
        with self.store.connection() as db:
            original=json.loads(db.execute("SELECT payload FROM records WHERE kind='source_revision' AND id=?",(doc['sources'][0]['id'],)).fetchone()[0])
        original_bytes=(self.store.directory/original['original_key']).read_bytes()
        self.dataset=services.create_set(self.user,'Imported area',self.scope.pk,'portable-target')
        self.assertTrue(self.bridge.once())
        sid=self.upload(original_bytes).json()['sources'][0]['id'];self.bridge.once()
        services.analyze_source(self.user,self.dataset.pk,sid,'portable-target-analysis');self.bridge.once()
        self.card=ExpertCard.objects.filter(source_id=sid).first()
        self.assertNotEqual(str(self.card.source_id),doc['sources'][0]['id'])
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        c=Command.objects.get(pk=r.json()['command_id']);self.assertEqual(c.state,'done')
        self.assertTrue(ExpertCard.objects.filter(source__normative_set=old_area,status='unreviewed').exists())
        profiles=DocumentProfile.objects.filter(normative_set=self.dataset,archived=False)
        self.assertEqual(profiles.count(),len(doc['profiles']))
        self.assertFalse(set(map(str,profiles.values_list('id',flat=True))) & {p['id'] for p in doc['profiles']})

    def independent_document(self):
        from .models import SourceUpload
        from knowledge_v2.store import checksum
        sid=self.prepared();source=SourceUpload.objects.get(pk=sid)
        with self.store.connection() as db:
            b=json.loads(db.execute("SELECT payload FROM records WHERE kind='fragment' AND json_extract(payload,'$.source_revision[0]')=? AND length(json_extract(payload,'$.exact_text'))>0 ORDER BY rowid LIMIT 1",(sid,)).fetchone()[0])
        pid=str(uuid.uuid4());cid=str(uuid.uuid4());proof=dict(locator=b['locator'],quote=b['exact_text'],start=0,end=len(b['exact_text']),context_hash=b['context_hash'],source_sha256=source.sha256)
        return dict(format='normcontrol.normative-area',version=1,exported_at='2026-09-28T00:00:00Z',area=dict(name='Independent',description='Source-backed independent fixture',automatic=False),sources=[dict(id=sid,filename=source.filename,sha256=source.sha256)],profiles=[dict(id=pid,definition=dict(name='Independent profile',description='Not generated by an LLM',parents=[],bindings=[],expression={'fact':{'name':'document_type','in':['ТЗ']}}))],requirements=[dict(id=cid,source_id=sid,payload=dict(description='Check the independently prepared duty.',entity_type='requirement',profile_id=pid,local_profile=pid,citations=[proof],conditions=[],exceptions=[],obligations=[],applicability={'fact':{'name':'document_type','in':['ТЗ']}},expert_approved=True,expert_status='confirmed',validation={'provenance':{'status':'verified'},'completeness':{'semantic':'verified'}}))],glossary=dict(entries=[],choices=[]),interdocument_requirements=[])

    def test_independent_import_without_analyze_checks_original_and_resets_trust(self):
        from .models import DocumentProfile
        doc=self.independent_document()
        self.assertFalse(Command.objects.filter(kind='source.analyze').exists())
        self.assertEqual(self.post(doc).status_code,200)
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        cmd=Command.objects.get(pk=r.json()['command_id']);self.assertEqual(cmd.state,'done',cmd.result)
        card=ExpertCard.objects.get(source__normative_set=self.dataset)
        self.assertEqual(card.analysis_id,cmd.pk);self.assertFalse(card.payload['expert_approved'])
        self.assertEqual(card.payload['validation']['completeness']['semantic'],'needs_review')
        self.assertTrue(card.contexts)
        self.assertNotEqual(card.payload['citations'][0]['context_hash'],doc['requirements'][0]['payload']['citations'][0]['context_hash'])
        self.assertTrue(DocumentProfile.objects.get(normative_set=self.dataset).definition['bindings'])
        self.assertEqual(self.post(doc,True).json()['command_id'],str(cmd.pk))
        self.assertEqual(ExpertCard.objects.count(),1)
        self.dataset.refresh_from_db();self.assertEqual(self.dataset.state,'prepared')
        self.assertIsNone(self.dataset.active_release_id)
        # Real canonical inspect must work, not only a fabricated UI projection.
        from .expert import submit
        submit(self.user,card.pk,dict(action='inspect',expected_revision=1,reason='Read imported proof.'),'inspect-import')
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())

    def test_independent_forged_nested_quote_rolls_back_whole_import(self):
        doc=self.independent_document();proof=dict(doc['requirements'][0]['payload']['citations'][0],quote='not in source')
        doc['requirements'][0]['payload']['conditions']=[dict(text='forged condition',citation=proof)]
        r=self.post(doc,True);self.assertEqual(r.status_code,202)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):
            with self.assertRaisesRegex(ValueError,'Source citation mismatch'):self.bridge.once()
        self.assertEqual(ExpertCard.objects.count(),0)
        self.assertEqual(Command.objects.get(pk=r.json()['command_id']).state,'failed')
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind IN ('expert_card','requirement','term_definition')").fetchone()[0],0)

    def test_independent_glossary_is_owned_by_area_without_document_profile(self):
        doc=self.independent_document();row=copy.deepcopy(doc['requirements'][0]);row['id']=str(uuid.uuid4())
        row['payload'].update(entity_type='definition',term='Термин',glossary_kind='term')
        for key in ('local_profile','profile_id'):row['payload'].pop(key,None)
        doc['glossary']=dict(entries=[row],choices=[dict(key='term:термин',active_entry_id=row['id'])])
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        self.assertEqual(Command.objects.get(pk=r.json()['command_id']).state,'done')
        card=ExpertCard.objects.get(entity_type='definition')
        self.assertNotIn('local_profile',card.payload)
        self.assertEqual(card.payload['profile_id'],'area-glossary')
        from .models import GlossaryGroup
        self.assertEqual(GlossaryGroup.objects.get(normative_set=self.dataset).active_id,card.pk)

    def test_independent_import_can_publish_without_fabricating_analysis_or_approval(self):
        from . import services
        from .models import Release
        from knowledge_v2.tests.test_search import FakeEncoder,FakeVectors
        from knowledge_v2.publication import material
        from knowledge_v2.review import ledger
        doc=self.independent_document()
        # An actual obligation is needed for a usable release ledger.
        card=doc['requirements'][0]['payload']
        card.update(modality='mandatory',composition={'atom':'duty'},obligations=[dict(id='duty',description='Check duty',action='check',subject='document',object='duty')])
        term=copy.deepcopy(doc['requirements'][0]);term['id']=str(uuid.uuid4())
        term['payload'].update(entity_type='definition',term='Термин',glossary_kind='term',obligations=[])
        for key in ('local_profile','profile_id'):term['payload'].pop(key,None)
        doc['glossary']=dict(entries=[term],choices=[dict(key='term:термин',active_entry_id=term['id'])])
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        self.dataset.refresh_from_db();self.bridge.normative_index=(FakeEncoder(),FakeVectors())
        c=services.prepare_selected_sources(self.user,self.dataset.pk,[str(self.dataset.sources.get().pk)],self.dataset.metadata_revision,'prepare-direct')
        self.assertEqual(c.payload['mode'],'imported_reference')
        from knowledge_v2.prepare import prepare
        from knowledge_v2.store import Conflict,checksum
        tampered=copy.deepcopy(c.payload)
        next(iter(tampered['imports'].values()))['material_digest']='0'*64
        tampered['versions']['import_selection_digest']=checksum(tampered['imports'])
        with self.assertRaisesRegex(Conflict,'Pinned import result differs'):
            prepare(self.store,str(uuid.uuid4()),tampered,FakeEncoder(),FakeVectors(),lambda *_:True)
        self.assertTrue(self.bridge.once());c.refresh_from_db();self.assertEqual(c.state,'done',c.result)
        release=Release.objects.get(pk=c.payload['release_id'])
        services.publish(self.user,self.dataset.pk,release.pk,self.dataset.metadata_revision,'publish-direct')
        self.assertTrue(self.bridge.once());self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.active_release_id,release.pk)
        policy=material(self.store,str(release.pk))
        self.assertFalse(policy['quality']['complete'])
        self.assertEqual(policy['quality']['counts']['candidate'],1)
        self.assertEqual(policy['quality']['counts']['reference'],1)
        self.assertTrue(all(x['trust']['preliminary_only'] for x in policy['catalog']))
        self.assertFalse(Command.objects.filter(kind='source.analyze').exists())
        rows=ledger(self.store,str(release.pk),list(policy['profiles'].values()),{},lambda *_:True,lambda *_:True)
        self.assertTrue(rows)
        self.assertTrue(all('critical_context_incomplete' in x['execution_issues'] for x in rows))
