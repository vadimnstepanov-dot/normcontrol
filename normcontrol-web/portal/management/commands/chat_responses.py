from django.core.management.base import BaseCommand
from portal.chat_model import drain

class Command(BaseCommand):
    help='Process durable model dialogue requests left in the portal queue.'
    def handle(self,*args,**options):drain()
