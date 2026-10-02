"""Notice for planned worker/gateway outages, without disabling worker callbacks."""
import json,os
from pathlib import Path
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from portal.maintenance import notice_html

class Command(BaseCommand):
    help='Включить или снять страницу плановых технических работ.'
    def add_arguments(self,parser):
        parser.add_argument('action',choices=['on','off','status'])
        parser.add_argument('--minutes',type=int,default=15)
        parser.add_argument('--message',default='Обновляем сервис NormControl.')
    def handle(self,*args,**options):
        path=Path(settings.DATA_DIR)/'maintenance.json'
        public_dir=getattr(settings,'MAINTENANCE_PUBLIC_DIR','')
        public=Path(public_dir)/'maintenance.html' if public_dir else None
        if options['action']=='off':
            if public:public.unlink(missing_ok=True)
            path.unlink(missing_ok=True);self.stdout.write('Технические работы завершены.');return
        if options['action']=='status':
            self.stdout.write(path.read_text(encoding='utf-8') if path.exists() else 'Страница работ выключена.');return
        if not 1<=options['minutes']<=1440:raise CommandError('Плановая длительность: 1–1440 минут.')
        end=timezone.localtime(timezone.now())+timedelta(minutes=options['minutes'])
        value={'active':True,'minutes':options['minutes'],'planned_end':end.isoformat(),'planned_end_label':end.strftime('%d.%m.%Y %H:%M %Z'),'message':options['message'][:1000]}
        if public:
            # Caddy serves only this generated public notice, even with Django stopped.
            public.parent.mkdir(parents=True,exist_ok=True)
            temporary=public.with_suffix('.tmp')
            with temporary.open('w',encoding='utf-8') as stream:
                stream.write(notice_html(value));stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o644);os.replace(temporary,public)
        temporary=path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        self.stdout.write('Страница работ включена до '+value['planned_end_label']+'.')
