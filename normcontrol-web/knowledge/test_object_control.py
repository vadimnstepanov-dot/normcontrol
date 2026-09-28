import uuid,json
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from .models import NormativeSet,Scope,SourceUpload,Command,ExpertCard,DocumentProfile,ObjectControl,AuditEvent
from .object_control import selected_cards,disabled_profiles

class ControlTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('expert');self.other=User.objects.create_user('other')
        self.scope=Scope.objects.create(owner=self.user,name='Private',kind='project')
        self.area=NormativeSet.objects.create(scope=self.scope,name='Rules',created_by=self.user,state='ready')
        self.source=SourceUpload.objects.create(normative_set=self.area,actor=self.user,filename='test.docx',sha256='a'*64,size=1,storage_key='x',state='prepared')
        self.command=Command.objects.create(normative_set=self.area,actor=self.user,kind='source.analyze',idempotency_key=uuid.uuid4().hex,payload={},state='done')
        self.card=ExpertCard.objects.create(source=self.source,analysis=self.command,base_id=uuid.uuid4(),payload={},description='Requirement',entity_type='requirement',profile_key='test')
        self.client.force_login(self.user)
    def change(self,kind,identity,action='deactivate',revision=0):
        return self.client.post(f'/normcontol/api/v2/areas/{self.area.pk}/objects/{kind}/{identity}/',json.dumps(dict(action=action,expected_revision=revision,reason='Expert decision')),content_type='application/json')
    def test_deactivation_blocks_new_selection_preserves_evidence_and_history(self):
        self.assertEqual(self.change('card',self.card.pk).status_code,200)
        self.assertEqual(selected_cards(self.area,[self.card]),[])
        self.assertTrue(ExpertCard.objects.filter(pk=self.card.pk).exists())
        self.area.refresh_from_db();self.assertEqual(self.area.state,'editing')
        self.assertEqual(AuditEvent.objects.filter(action='object.deactivate').count(),1)
        self.assertEqual(self.change('card',self.card.pk,'activate',1).status_code,200)
        self.assertEqual(selected_cards(self.area,[self.card]),[self.card])
    def test_stale_unauthorized_and_foreign_objects_rejected(self):
        self.assertEqual(self.change('card',self.card.pk,revision=7).status_code,409)
        self.client.force_login(self.other);self.assertEqual(self.change('card',self.card.pk).status_code,403)
        self.assertFalse(ObjectControl.objects.exists())
    def test_busy_publication_cannot_change_selection(self):
        self.command.state='pending';self.command.save()
        self.assertEqual(self.change('source',self.source.pk).status_code,409)
    def test_deleted_source_excludes_cards_but_keeps_original(self):
        self.assertEqual(self.change('source',self.source.pk,'delete').status_code,200)
        self.assertEqual(selected_cards(self.area,[self.card]),[])
        self.source.refresh_from_db();self.assertEqual(self.source.storage_key,'x')
        self.assertEqual(self.change('source',self.source.pk,'activate',1).status_code,409)
    def test_parent_deactivation_applies_to_descendants(self):
        parent=DocumentProfile.objects.create(scope=self.scope,normative_set=self.area,name='Parent',definition={'parents':[]})
        child=DocumentProfile.objects.create(scope=self.scope,normative_set=self.area,name='Child',definition={'parents':[str(parent.pk)],'bindings':[{'source_id':str(self.source.pk),'profile_id':'test'}]})
        self.assertEqual(self.change('profile',parent.pk).status_code,200)
        self.assertIn(str(child.pk),disabled_profiles(self.area));self.assertEqual(selected_cards(self.area,[self.card]),[])
