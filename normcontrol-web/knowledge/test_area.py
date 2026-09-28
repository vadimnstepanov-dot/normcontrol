import json, uuid
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from .models import Scope,NormativeSet,DocumentProfile,SourceUpload,Command,ExpertCard,ExpertCardRevision,ExperienceReview
from .profiles import save_profile
from . import area,services


class AreaTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('area-owner')
        self.reader=User.objects.create_user('area-outsider')
        self.scope=Scope.objects.create(owner=self.user,name='ACL',kind='project')
        self.area=NormativeSet.objects.create(scope=self.scope,name='Area',created_by=self.user)
        self.client.force_login(self.user)
    def post(self,url,data):return self.client.post('/normcontol/api/v2/'+url,json.dumps(data),content_type='application/json',HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex)
    def definition(self,name='Profile',parents=None):return dict(name=name,description='For technical documents',expression={'fact':{'name':'document_type','in':['ОИТ']}},parents=parents or [],bindings=[])
    def test_business_area_separate_from_acl_and_automatic(self):
        r=self.post('areas/',dict(name='New area',scope_id=str(self.scope.pk)))
        self.assertEqual(r.status_code,201)
        created=NormativeSet.objects.get(pk=r.json()['id'])
        self.assertTrue(created.automatic);self.assertEqual(created.scope,self.scope)
    def test_nonadmin_expert_can_edit_profiles_only_in_authorized_area(self):
        p=save_profile(self.user,self.scope.pk,self.definition(),dataset=self.area)
        self.assertEqual(p.normative_set,self.area)
        with self.assertRaises(PermissionDenied):save_profile(self.reader,self.scope.pk,self.definition(),dataset=self.area)
    def test_profile_cannot_inherit_another_area(self):
        p=save_profile(self.user,self.scope.pk,self.definition(),dataset=self.area)
        other=NormativeSet.objects.create(scope=self.scope,name='Other',created_by=self.user)
        with self.assertRaises(ValueError):save_profile(self.user,self.scope.pk,self.definition('child',[str(p.pk)]),dataset=other)
    def test_delete_preserves_profile_revisions_and_refuses_children(self):
        p=save_profile(self.user,self.scope.pk,self.definition(),dataset=self.area)
        save_profile(self.user,self.scope.pk,self.definition('child',[str(p.pk)]),dataset=self.area)
        r=self.post(f'areas/{self.area.pk}/profiles/{p.pk}/delete/',dict(expected_revision=1,reason='Not applicable'))
        self.assertEqual(r.status_code,409)
        child=self.area.profiles.get(name='child')
        r=self.post(f'areas/{self.area.pk}/profiles/{child.pk}/delete/',dict(expected_revision=1,reason='Not applicable'))
        self.assertEqual(r.status_code,200);child.refresh_from_db();self.assertTrue(child.archived);self.assertEqual(child.revisions.count(),2)
    def test_reader_cannot_fetch_private_tree(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(f'/normcontol/api/v2/areas/{self.area.pk}/').status_code,403)
        self.assertEqual(self.client.get('/normcontol/api/v2/areas/').json()['areas'],[])
    def test_expert_review_in_correct_card_and_reject(self):
        r=ExperienceReview.objects.create(normative_set=self.area,author=self.user,submission=dict(card_id='card',comment='Incorrect rule'))
        result=self.client.get(f'/normcontol/api/v2/areas/{self.area.pk}/reviews/')
        self.assertEqual(len(result.json()['entries']),1)
        result=self.post(f'areas/{self.area.pk}/reviews/{r.pk}/',dict(action='reject',expected_revision=1,reason='Original rule is correct'))
        self.assertEqual(result.status_code,200);r.refresh_from_db();self.assertEqual(r.state,'rejected')
    def test_automatic_ingestion_enqueues_semantic_analysis_once(self):
        self.area.automatic=True;self.area.save()
        s=SourceUpload.objects.create(normative_set=self.area,actor=self.user,filename='test.docx',sha256='a'*64,size=100,storage_key='x',state='prepared')
        from .automatic import advance
        advance(self.area,self.user);advance(self.area,self.user)
        self.assertEqual(Command.objects.filter(kind='source.analyze').count(),1)
        self.assertTrue(Command.objects.get(kind='source.analyze').payload['automatic_screening'])
