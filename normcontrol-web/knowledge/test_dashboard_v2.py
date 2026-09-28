import uuid
from django.test import TestCase,override_settings
from django.contrib.auth.models import User
from portal.models import Batch
from .models import Scope,NormativeSet,Snapshot,KnowledgeCheck

@override_settings(KNOWLEDGE_V2_ENABLED=True)
class DashboardV2Tests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('viewer')
        self.other=User.objects.create_user('scope-owner')
        self.batch=Batch.objects.create(owner=self.user,name='Package')
        self.scope=Scope.objects.create(owner=self.user,name='Rules',kind='project')
        self.rules=NormativeSet.objects.create(scope=self.scope,name='Rules',created_by=self.user)
        self.snapshot=Snapshot.objects.create(owner=self.user,job_id=uuid.uuid4(),digest='a'*64,
            data={'releases':[{'set_id':str(self.rules.pk)}]})
        self.job=KnowledgeCheck.objects.create(owner=self.user,batch=self.batch,snapshot=self.snapshot,
            state='partial',progress={'completed':12,'total':12,'percent':100})
        self.client.force_login(self.user)
    def test_selected_check_is_truthful_and_uses_v2_registry(self):
        response=self.client.get('/normcontol/',{'batch':str(self.batch.pk)})
        self.assertEqual(response.context['selected_knowledge'].pk,self.job.pk)
        self.assertContains(response,'Завершена с ограничениями')
        self.assertContains(response,'data-job="'+str(self.job.pk)+'"')
        self.assertContains(response,'kn-trace-matrix')
        self.assertNotContains(response,'id="review-progress"')
        self.batch.refresh_from_db();self.assertEqual(self.batch.status,'prepared')
    def test_revoked_scope_hides_v2_results(self):
        self.scope.owner=self.other;self.scope.save()
        response=self.client.get('/normcontol/',{'batch':str(self.batch.pk)})
        self.assertIsNone(response.context['selected_knowledge'])
        self.assertNotContains(response,str(self.job.pk))
        self.assertContains(response,'id="review-progress"')
    def test_live_v2_preferred_without_overriding_explicit_selection(self):
        self.job.state='running';self.job.save()
        other=Batch.objects.create(owner=self.user,name='Other')
        self.assertEqual(self.client.get('/normcontol/').context['selected_batch'].pk,self.batch.pk)
        self.assertEqual(self.client.get('/normcontol/',{'batch':str(other.pk)}).context['selected_batch'].pk,other.pk)
