"""Bounded CPU work inside the portal, using persisted export states and plans."""
from concurrent.futures import ThreadPoolExecutor
from django.db import transaction,close_old_connections
from .models import WordReviewExport,LLMRuntime
POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='word-export')
def kick():return POOL.submit(drain)
def drain():
 from .review_export_views import build_export,WaitingForMemory
 close_old_connections()
 try:
  while True:
   with transaction.atomic():
    # Reuse the portal's singleton coordination row for a short claim lock.
    LLMRuntime.objects.get_or_create(pk=1)
    LLMRuntime.objects.select_for_update().get(pk=1)
    if WordReviewExport.objects.filter(state='building').exists():return
    record=WordReviewExport.objects.filter(state='queued').order_by('created').first()
    if record is None:return
    record.state='building';record.save(update_fields=['state'])
   try:build_export(record)
   except WaitingForMemory as e:
    record.state='waiting_ram';record.error=str(e);record.save(update_fields=['state','error']);return
   except Exception as e:
    record.state='failed';record.error=str(e);record.save(update_fields=['state','error'])
 finally:close_old_connections()
