"""Portal orchestration of v2 checks; all model work remains on the local worker."""
import uuid
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from portal.access import can_edit, visible_batches
from portal.models import Batch
from .access import allowed,require
from .models import NormativeSet,Release,KnowledgeCheck,KnowledgeFindingChunk,Command
from .services import create_snapshot,command,digest,audit,Conflict,NotReady


def visible(user,job):
    if not visible_batches(user).filter(pk=job.batch_id).exists():raise PermissionDenied('Batch access required')
    for item in job.snapshot.data['releases']:
        require(user,NormativeSet.objects.get(pk=item['set_id']).scope,'read')
    if job.experience_release_id:require(user,job.experience_release.normative_set.scope,'read')
    return job


@transaction.atomic
def start(user,batch_id,set_ids,experience_set_id,key,*,workflow=None):
    batch=Batch.objects.select_for_update().get(pk=batch_id)
    if not can_edit(user,batch) or batch.archived:raise PermissionDenied('Batch owner required')
    if not isinstance(set_ids,list) or not 1<=len(set_ids)<=20 or len(set_ids)!=len(set(set_ids)):
        raise ValueError('Choose one to twenty unique normative sets')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if workflow is not None and (not isinstance(workflow,dict) or set(workflow)-{'unified_launch','after_non_normative','directions','pipeline_version','execution_order'} or not {'unified_launch','after_non_normative','directions'}<=set(workflow) or workflow.get('pipeline_version','pipeline-v1')!='pipeline-v1' or workflow.get('execution_order','parallel') not in ('parallel','sto_first')):
        raise ValueError('Invalid workflow metadata')
    identity=digest(['check-start',user.pk,key]);old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        if old.payload.get('batch_id')!=str(batch.pk) or old.payload.get('set_ids')!=sorted(set_ids) or old.payload.get('experience_set_id')!=experience_set_id:
            raise Conflict('Check retry differs')
        return KnowledgeCheck.objects.get(pk=old.payload['job_id']),old
    if KnowledgeCheck.objects.filter(batch=batch,state__in=['queued','running','paused']).exists():
        raise Conflict('A v2 check for this batch is already active')
    selected=list(NormativeSet.objects.select_related('scope','active_release').filter(pk__in=set_ids))
    if len(selected)!=len(set_ids) or any(x.purpose!='normative' or x.state!='ready' or not x.active_release or x.active_release.state!='active' for x in selected):
        raise NotReady('Selected normative sets are not published')
    if any(not x.active_release.manifest.get('versions',{}).get('curation_digest') for x in selected):
        raise NotReady('Подготовьте и опубликуйте выпуск с экспертными версиями. Старые проверки сохраняют свой снимок.')
    scopes={str(x.scope_id) for x in selected}
    for x in selected:require(user,x.scope,'read')
    experience=None
    if experience_set_id:
        lessons=NormativeSet.objects.select_related('scope','active_release').get(pk=experience_set_id)
        require(user,lessons.scope,'read')
        if lessons.purpose!='experience' or lessons.state!='ready' or str(lessons.scope_id) not in scopes or not lessons.active_release or lessons.active_release.state!='active':
            raise NotReady('Selected experience set is not published in this scope')
        experience=lessons.active_release
    job_id=uuid.uuid4();snap=create_snapshot(user,job_id,set_ids,{'engine':'review-v2.3','planner':'context-budget-v5','template':'sto-template-v1','visual':'visual-tail-v1','trace':'package-trace-9.1.6','response_schema':'review-v2','selection':'explicit'})
    job=KnowledgeCheck.objects.create(id=job_id,batch=batch,owner=user,snapshot=snap,experience_release=experience,
        progress={'completed':0,'total':None,'percent':0,'eta_seconds':None})
    docs=list(batch.documents.order_by('id').values('id','name','size','sha256','review_role'))
    if not docs:raise NotReady('Batch contains no documents')
    payload=dict(set_id=str(selected[0].pk),actor_id=user.pk,job_id=str(job.pk),batch_id=str(batch.pk),
        snapshot_id=str(snap.pk),snapshot=snap.data,snapshot_digest=snap.digest,
        set_ids=sorted(set_ids),experience_set_id=experience_set_id,
        experience_release_id=str(experience.pk) if experience else None,experience_scope_id=str(experience.normative_set.scope_id) if experience else sorted(scopes)[0],
        documents=docs,template_version='sto-template-v1',trace_version='package-trace-9.1.6',planning_version='context-budget-v5',visual_version='visual-tail-v1',**(workflow or {}))
    payload['logging']={'enabled':batch.logging_enabled,'version':'check-log-v1','directions':list((workflow or {}).get('directions',batch.checks))}
    c=command(user,selected[0],'review.execute',identity,payload)
    audit(user,'knowledge_check.queued',job.pk,{'sets':len(selected),'documents':len(docs)})
    return job,c


@transaction.atomic
def control(user,job_id,action):
    job=KnowledgeCheck.objects.select_for_update().select_related('batch').get(pk=job_id)
    visible(user,job)
    if not can_edit(user,job.batch):raise PermissionDenied('Check owner required')
    if action=='pause':
        if job.state not in ('queued','running'):raise Conflict('Check is not running')
        job.pause_requested=True;job.save(update_fields=['pause_requested'])
        if job.state=='queued':
            job.state='paused';job.save(update_fields=['state'])
            Command.objects.filter(kind='review.execute',state='pending',payload__job_id=str(job.pk)).update(state='done',result={'reason':'paused_before_execution'})
    elif action in ('resume','retry'):
        if job.state!=('paused' if action=='resume' else 'failed'):
            raise Conflict('Check is not available for '+action)
        job.pause_requested=False;job.state='queued';job.save(update_fields=['pause_requested','state'])
        previous=Command.objects.filter(kind='review.execute',payload__job_id=str(job.pk)).order_by('-created').first()
        payload=dict(previous.payload)
        command(user,previous.normative_set,'review.execute',digest(['check-resume',str(job.pk),job.progress.get('completed',0),uuid.uuid4().hex]),payload)
    else:raise ValueError('Unsupported check control')
    audit(user,'knowledge_check.'+action,job.pk)
    from .launch import reconcile
    reconcile(job.batch)
    return job


@transaction.atomic
def progress(worker_id,command_id,lease,job_id,value):
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='review.execute')
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now()
        or c.payload['job_id']!=str(job_id)):raise Conflict('Stale check lease')
    require(c.actor,c.normative_set.scope,'read')
    job=KnowledgeCheck.objects.select_for_update().get(pk=job_id)
    if (not isinstance(value,dict) or not {'completed','total','percent','eta_seconds','task_id','preview'}<=set(value)
        or set(value)-{'completed','total','percent','eta_seconds','task_id','preview','stage','visual_total','operation','template_comparison'}):
        raise ValueError('Progress schema')
    if 'stage' in value and value['stage'] not in ('download','parse','classify','facts','template','select','plan','visual_plan','text','trace','vision'):raise ValueError('Progress stage')
    if 'template_comparison' in value:
        comparison=value['template_comparison']
        if not isinstance(comparison,dict) or comparison.get('version')!='sto-template-v1' or not isinstance(comparison.get('documents'),list):raise ValueError('Template comparison schema')
    if 'visual_total' in value and (type(value['visual_total']) is not int or value['visual_total']<0):raise ValueError('Visual task count')
    if type(value['completed']) is not int or value['completed']<job.progress.get('completed',0):raise Conflict('Progress cannot rewind')
    if value['total'] is None:
        if value['completed']!=0 or job.progress.get('total') is not None:raise ValueError('Preparation progress cannot replace a plan')
    elif type(value['total']) is not int or value['total']<value['completed']:raise ValueError('Progress total')
    if 'operation' in value and (not isinstance(value['operation'],str) or len(value['operation'])>300):raise ValueError('Progress operation')
    if not isinstance(value['preview'],list) or len(value['preview'])>5 or any(not isinstance(x,dict) or x.get('state')!='candidate' for x in value['preview']):
        raise ValueError('Preview schema')
    if 'template_comparison' not in value and job.progress.get('template_comparison'):
        value=dict(value,template_comparison=job.progress['template_comparison'])
    job.progress=dict(value,updated_at=timezone.now().isoformat())
    if job.state=='queued':job.state='running'
    job.save(update_fields=['progress','state'])
    return {'accepted':True,'pause_requested':job.pause_requested}


@transaction.atomic
def add_findings(worker_id,command_id,lease,job_id,sequence,entries,entry_digest):
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='review.execute')
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now()
        or c.payload['job_id']!=str(job_id)):raise Conflict('Stale check lease')
    require(c.actor,c.normative_set.scope,'read')
    if type(sequence) is not int or sequence<0 or not isinstance(entries,list) or not 1<=len(entries)<=20 or digest(entries)!=entry_digest:
        raise ValueError('Finding chunk schema')
    job=KnowledgeCheck.objects.get(pk=job_id)
    row,created=KnowledgeFindingChunk.objects.get_or_create(job=job,sequence=sequence,defaults={'entries':entries,'digest':entry_digest})
    if not created and row.digest!=entry_digest:raise Conflict('Finding chunk collision')
    return {'accepted':True}
