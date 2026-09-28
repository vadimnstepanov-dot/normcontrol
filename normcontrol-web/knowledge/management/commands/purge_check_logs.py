from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Max
from knowledge.models import CheckLogChunk, KnowledgeCheck
from portal.models import BatchLogChunk, Batch

class Command(BaseCommand):
    help='Remove expired detailed journals of terminal checks; preserve reports, snapshots and paused jobs.'
    def add_arguments(self,parser):
        parser.add_argument('--days',type=int,default=getattr(settings,'CHECK_LOG_RETENTION_DAYS',30))
        parser.add_argument('--dry-run',action='store_true')
    def handle(self,*args,**opts):
        if not 1<=opts['days']<=3650:raise ValueError('Retention days')
        cutoff=timezone.now()-timedelta(days=opts['days'])
        states=['completed','partial','failed','cancelled']
        # Expire whole streams; deleting only an old prefix corrupts a resumed journal.
        jobs=KnowledgeCheck.objects.filter(state__in=states).annotate(last_log=Max('log_chunks__created')).filter(last_log__lt=cutoff)
        batches=Batch.objects.filter(status__in=states).annotate(last_log=Max('log_chunks__created')).filter(last_log__lt=cutoff)
        queries=[CheckLogChunk.objects.filter(job__in=jobs),BatchLogChunk.objects.filter(batch__in=batches)]
        for query in queries:
            count=query.count()
            if not opts['dry_run']:query.delete()
            self.stdout.write(f'{query.model.__name__}: {count}; dry_run={opts["dry_run"]}')
