"""Queue demand wakes the desktop model without claiming or resuming checks."""
import uuid
from datetime import timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Batch, WorkerRun, ChatResponse


def pending_demand(after=None):
    batches=Batch.objects.filter(status='waiting',archived=False)
    if after:batches=batches.filter(created__gt=after)
    batch=batches.order_by('queue_position','created').first()
    if batch:return 'batch:'+str(batch.pk)
    responses=ChatResponse.objects.filter(state__in=['queued','running'],user_message__conversation__owner__is_active=True)
    if after:responses=responses.filter(created__gt=after)
    response=responses.order_by('created').first()
    if response:return 'chat:'+str(response.pk)
    from django.apps import apps
    if not apps.is_installed('knowledge'):return None
    from knowledge.models import Command, KnowledgeCheck
    commands=Command.objects.filter(kind='review.execute',state__in=['pending','delivering']).order_by('created')
    if after:commands=commands.filter(created__gt=after)
    for command in commands.iterator():
        job=KnowledgeCheck.objects.filter(pk=command.payload.get('job_id'),state__in=['queued','running'],
            pause_requested=False,batch__archived=False).exclude(batch__status__in=['paused','cancelled']).first()
        if not job:continue
        if command.payload.get('after_non_normative') and not WorkerRun.objects.filter(
                batch=job.batch,state__in=['completed','partial']).exists():continue
        return 'check:'+str(job.pk)
    return None


def queue_start(runtime,now=None):
    if ChatResponse.objects.filter(state__in=['queued','running']).exists():
        from django.db import transaction
        from .chat_model import kick
        transaction.on_commit(kick)
    now=now or timezone.now()
    command=dict(runtime.command or {})
    if command.get('state')=='pending':return command
    latest=(runtime.history or [])[-1].get('at') if runtime.history else None
    sampled=parse_datetime(latest) if latest else None
    if sampled and now-sampled<timedelta(seconds=90) and (runtime.sample or {}).get('online'):return command
    # An explicit stop applies to existing work. A later new task may wake the model.
    cutoff=parse_datetime(command.get('created','')) if command.get('action')=='stop' else None
    if command.get('state')=='failed':
        finished=parse_datetime(command.get('finished',''))
        if finished and now-finished<timedelta(minutes=5):return command
    demand=pending_demand(cutoff)
    if not demand:return command
    return dict(id=uuid.uuid4().hex,action='start',state='pending',automatic=True,
        demand=demand,created=now.isoformat())


def command_reply(command):
    if not command or command.get('state')!='pending':return None
    result={'id':command['id'],'action':command['action']}
    if command.get('automatic'):result['automatic']=True
    return result
