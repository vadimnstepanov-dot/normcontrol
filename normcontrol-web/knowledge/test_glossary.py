import json,uuid
from datetime import timedelta
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Scope,NormativeSet,SourceUpload,Command,ExpertCard,GlossaryGroup
from .glossary import sync,frozen


class GlossaryTests(TestCase):
    def setUp(self):
        self.owner=User.objects.create_user('glossary-expert');self.outsider=User.objects.create_user('outside')
        self.scope=Scope.objects.create(owner=self.owner,name='ACL',kind='project')
        self.area=NormativeSet.objects.create(scope=self.scope,created_by=self.owner,name='Normative area')
        self.client.force_login(self.owner)
    def card(self,name,value,kind='term',old=False,area=None):
        area=area or self.area
        s=SourceUpload.objects.create(normative_set=area,actor=self.owner,filename='example.docx',sha256=uuid.uuid4().hex*2,size=10,storage_key='test')
        if old:SourceUpload.objects.filter(pk=s.pk).update(created=timezone.now()-timedelta(days=1))
        c=Command.objects.create(normative_set=area,actor=self.owner,kind='source.analyze',payload={},digest='a'*64,idempotency_key=uuid.uuid4().hex)
        return ExpertCard.objects.create(source=s,analysis=c,base_id=uuid.uuid4(),payload=dict(term=name,glossary_kind=kind,entity_type='definition',description=value),description=value,entity_type='definition')
    def post(self,c,g,**extra):
        return self.client.post(f'/normcontol/api/v2/areas/{self.area.pk}/glossary/{c.pk}/activate/',json.dumps(dict(dict(expected_revision=c.revision,group_revision=g.revision,reason='Exact source is applicable'),**extra)),content_type='application/json')
    def test_load_order_not_completion_order_and_explicit_choice(self):
        later=self.card('RPO','24 hours');sync(self.area)
        first=self.card('rpo','12 hours',old=True);sync(self.area)
        g=GlossaryGroup.objects.get();self.assertEqual(g.active_id,first.pk);self.assertTrue(g.conflict)
        self.assertEqual(self.post(later,g).status_code,200);g.refresh_from_db()
        self.assertEqual(g.active_id,later.pk);self.assertTrue(g.expert_selected)
        self.card('RPO','8 hours',old=True);sync(self.area);g.refresh_from_db();self.assertEqual(g.active_id,later.pk)
        self.assertEqual(frozen(self.area,list(ExpertCard.objects.select_related('source').all()))[0]['active'],str(later.pk))
        r=self.client.get(f'/normcontol/api/v2/areas/{self.area.pk}/glossary/?state=inactive').json()
        self.assertEqual(r['total'],2);self.assertIn('uploaded',r['entries'][0]);self.assertFalse(r['entries'][0]['active'])
    def test_all_types_and_equal_definitions_not_conflicts(self):
        for kind in ('term','symbol','abbreviation'):
            self.card('X','Value',kind);self.card('X','Value',kind)
        sync(self.area);self.assertEqual(GlossaryGroup.objects.count(),3)
        self.assertFalse(GlossaryGroup.objects.filter(conflict=True).exists())
        r=self.client.get(f'/normcontol/api/v2/areas/{self.area.pk}/glossary/?kind=symbol').json();self.assertEqual(r['total'],2)
        self.assertEqual(r['entries'][0]['kind'],'symbol')
    def test_acl_area_and_stale_choice(self):
        a=self.card('Term','First');b=self.card('Term','Second');sync(self.area);g=GlossaryGroup.objects.get()
        self.assertEqual(self.post(b,g,group_revision=999).status_code,409)
        self.client.force_login(self.outsider);self.assertEqual(self.post(b,g).status_code,403)
        self.assertEqual(self.client.get(f'/normcontol/api/v2/areas/{self.area.pk}/glossary/').status_code,403)
    def test_delete_active_and_edit_name_keep_history_projection(self):
        a=self.card('Term','First',old=True);b=self.card('Term','Second');sync(self.area)
        a.status='rejected';a.save();sync(self.area);g=GlossaryGroup.objects.get();self.assertEqual(g.active_id,b.pk)
        b.payload=dict(b.payload,term='Other');b.save();sync(self.area)
        self.assertIsNone(GlossaryGroup.objects.get(key='term:term').active_id)
        self.assertEqual(GlossaryGroup.objects.get(key='term:other').active_id,b.pk)
    def test_new_source_revision_does_not_leave_old_active(self):
        a=self.card('Term','First',old=True);b=self.card('Term','New');b.source.supersedes=a.source;b.source.save()
        sync(self.area);self.assertEqual(GlossaryGroup.objects.get().active_id,b.pk)
    def test_choice_waits_publication_boundary(self):
        a=self.card('Term','First');sync(self.area);g=GlossaryGroup.objects.get()
        Command.objects.create(normative_set=self.area,actor=self.owner,kind='release.prepare',payload={},digest='b'*64,idempotency_key=uuid.uuid4().hex)
        self.assertEqual(self.post(a,g).status_code,409)
