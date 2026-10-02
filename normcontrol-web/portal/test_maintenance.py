import json,tempfile
from pathlib import Path
from django.http import HttpResponse
from django.test import SimpleTestCase,RequestFactory,override_settings
from django.core.management import call_command
from .maintenance import MaintenanceMiddleware

@override_settings(MAINTENANCE_PUBLIC_DIR='')
class MaintenanceTests(SimpleTestCase):
    def test_notice_survives_restart_and_preserves_worker_transport(self):
        with tempfile.TemporaryDirectory() as directory,override_settings(DATA_DIR=Path(directory)):
            call_command('planned_maintenance','on',minutes=15,message='<script>test</script>')
            factory=RequestFactory();middleware=MaintenanceMiddleware(lambda request:HttpResponse('worker'))
            response=middleware(factory.get('/normcontol/chat/'))
            self.assertEqual(response.status_code,503);self.assertEqual(response['Retry-After'],'30')
            self.assertContains(response,'Идут технические работы',status_code=503)
            self.assertNotIn(b'<script>',response.content)
            for path in ('/normcontol/worker/ping/','/normcontol/knowledge/worker/checks/progress/','/normcontol/static/chat.js'):
                self.assertEqual(middleware(factory.post(path)).status_code,200)
            self.assertEqual(MaintenanceMiddleware(lambda request:HttpResponse())(factory.get('/normcontol/')).status_code,503)
            call_command('planned_maintenance','off');self.assertEqual(middleware(factory.get('/normcontol/')).status_code,200)
    def test_malformed_notice_does_not_disable_service(self):
        with tempfile.TemporaryDirectory() as directory,override_settings(DATA_DIR=Path(directory)):
            (Path(directory)/'maintenance.json').write_text('bad')
            response=MaintenanceMiddleware(lambda request:HttpResponse())(RequestFactory().get('/normcontol/'))
            self.assertEqual(response.status_code,200)
    def test_proxy_notice_is_escaped_persistent_and_removed_after_work(self):
        with tempfile.TemporaryDirectory() as directory:
            public=Path(directory)/'public'
            with override_settings(DATA_DIR=Path(directory),MAINTENANCE_PUBLIC_DIR=public):
                call_command('planned_maintenance','on',minutes=12,message='<script>bad</script>')
                page=(public/'maintenance.html').read_text(encoding='utf-8')
                self.assertIn('12 мин',page);self.assertIn('http-equiv="refresh"',page)
                self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)
                call_command('planned_maintenance','on',minutes=20,message='Уточнённый срок')
                self.assertIn('20 мин',(public/'maintenance.html').read_text(encoding='utf-8'))
                call_command('planned_maintenance','off')
                self.assertFalse((public/'maintenance.html').exists())
                self.assertFalse((Path(directory)/'maintenance.json').exists())
