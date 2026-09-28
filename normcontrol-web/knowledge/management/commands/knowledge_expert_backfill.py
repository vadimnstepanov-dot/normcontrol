from django.core.management.base import BaseCommand
from django.db import transaction
from knowledge.models import Command,SourceUpload
from knowledge.expert import project


class Command(BaseCommand):
    help='Index already accepted analysis projections; never run LLM or overwrite expert history.'
    def handle(self,*args,**options):
        from knowledge.models import Command as Job
        count=0
        for job in Job.objects.filter(kind='source.analyze',state='done').order_by('created','id'):
            source=SourceUpload.objects.get(pk=job.payload['source_id'])
            with transaction.atomic():
                entries=[row for chunk in job.analysis_chunks.order_by('sequence') for row in chunk.entries]
                project(job,source,entries);count+=len(entries)
        self.stdout.write(f'Indexed {count} accepted cards; originals and expert revisions preserved.')
