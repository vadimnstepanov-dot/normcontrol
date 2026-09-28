from django.core.management.base import BaseCommand
from knowledge.models import NormativeSet
from knowledge.glossary import sync


class Command(BaseCommand):
    help='Build glossary projection from existing verified extraction cards; no LLM or publication.'
    def handle(self,*args,**kwargs):
        for area in NormativeSet.objects.filter(purpose='normative').iterator():sync(area)
        self.stdout.write('Glossary projection rebuilt; check snapshots and queues unchanged.')
