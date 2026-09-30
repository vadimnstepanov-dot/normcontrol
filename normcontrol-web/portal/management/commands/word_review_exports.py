"""Resume persisted exports after a portal restart without changing their plans."""
from django.core.management.base import BaseCommand
from portal.models import WordReviewExport
from portal.review_export_queue import drain
class Command(BaseCommand):
 help='Process the saved Word export queue; recover interrupted exports only when no generator is running.'
 def add_arguments(self,parser):parser.add_argument('--recover-interrupted',action='store_true')
 def handle(self,*args,**options):
  if options['recover_interrupted']:
   count=WordReviewExport.objects.filter(state='building').update(state='queued')
   self.stdout.write('Recovered interrupted exports: '+str(count))
  drain();self.stdout.write('Word export queue processed')
