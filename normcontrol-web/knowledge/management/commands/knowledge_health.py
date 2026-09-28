from django.core.management.base import BaseCommand
from knowledge.models import Scope, NormativeSet


class Command(BaseCommand):
    help='Check the v2 schema without creating or changing data'

    def handle(self,*args,**options):
        Scope.objects.count()
        NormativeSet.objects.count()
        self.stdout.write('knowledge-v2 schema ready')
