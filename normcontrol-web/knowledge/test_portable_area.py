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
        self.dataset=services.create_set(self.user,'Imported area',self.scope.pk,'portable-target')
        self.assertTrue(self.bridge.once())
        sid=self.prepared();services.analyze_source(self.user,self.dataset.pk,sid,'portable-target-analysis');self.bridge.once()
        self.card=ExpertCard.objects.filter(source_id=sid).first()
        self.assertNotEqual(str(self.card.source_id),doc['sources'][0]['id'])
        r=self.post(doc,True);self.assertEqual(r.status_code,202,r.content)
        with patch.dict(os.environ,KNOWLEDGE_EXPERT_ONLY='1'):self.assertTrue(self.bridge.once())
        c=Command.objects.get(pk=r.json()['command_id']);self.assertEqual(c.state,'done')
        self.assertTrue(ExpertCard.objects.filter(source__normative_set=old_area,status='unreviewed').exists())
        profiles=DocumentProfile.objects.filter(normative_set=self.dataset,archived=False)
        self.assertEqual(profiles.count(),len(doc['profiles']))
        self.assertFalse(set(map(str,profiles.values_list('id',flat=True))) & {p['id'] for p in doc['profiles']})
