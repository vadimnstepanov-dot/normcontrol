import hashlib
import json
import uuid
from datetime import timedelta
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from .access import allowed, require, ROLE_PERMISSIONS
from .models import Scope, Membership, NormativeSet, Release, Snapshot, Command, Receipt, AuditEvent, SourceUpload, CoverageChunk, AnalysisChunk, KnowledgeCheck


class Conflict(ValueError): pass
class NotReady(ValueError): pass


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def audit(actor, action, aggregate, data=None):
    AuditEvent.objects.create(actor=actor,action=action,aggregate_id=str(aggregate),data=data or {})


def require_integrated_expert_drafts(dataset):
    # Stage 9.1.5 will pin expert versions in release manifests. Until then do
    # not silently publish the original model output over a human decision.
    from .models import ExpertCard
    if (ExpertCard.objects.filter(source__normative_set=dataset).filter(
            Q(revision__gt=1)|Q(history__action__in=['split','merge'])).exists()
        or Command.objects.filter(normative_set=dataset,kind='expert.apply',
            state__in=['pending','delivering']).exclude(payload__action='inspect').exists()):
        raise NotReady('Экспертные изменения сохранены в черновиках. Включение этих версий в нормативный выпуск выполняется на этапе 9.1.5.')


def command(user, dataset, kind, key, payload):
    if not isinstance(key,str) or not 1<=len(key)<=160:raise ValueError('Idempotency-Key required (1–160 characters)')
    h=digest(dict(kind=kind,set_id=str(dataset.pk),actor=user.pk,payload=payload))
    old=Command.objects.filter(idempotency_key=key).first()
    if old:
        if old.digest!=h:raise Conflict('Idempotency key conflict')
        return old
    return Command.objects.create(normative_set=dataset,actor=user,kind=kind,idempotency_key=key,payload=payload,digest=h)


@transaction.atomic
def create_scope(user, name, kind, parent_id=None):
    if not user.is_authenticated or not user.is_active:raise PermissionDenied('Active account required')
    if not isinstance(name,str) or not name.strip() or len(name)>160:raise ValueError('Scope name required')
    if kind not in dict(Scope.KINDS):raise ValueError('Scope kind')
    parent=Scope.objects.get(pk=parent_id) if parent_id else None
    if kind=='personal':
        if parent:raise ValueError('Personal scope has no parent')
        scope,_=Scope.objects.get_or_create(owner=user,kind=kind,defaults={'name':name})
        return scope
    if parent:
        require(user,parent,'manage')
        if (kind,parent.kind) not in {('team','organization'),('project','team'),('project','organization')}:
            raise ValueError('Invalid scope hierarchy')
    elif not user.is_staff:
        raise PermissionDenied('Root shared scope requires administrator')
    s=Scope.objects.create(owner=user,name=name.strip(),kind=kind,parent=parent)
    audit(user,'scope.created',s.pk)
    return s


@transaction.atomic
def set_membership(actor, scope_id, user, role):
    scope=Scope.objects.select_for_update().get(pk=scope_id)
    require(actor,scope,'manage')
    if scope.kind=='personal':raise ValueError('Personal knowledge is not a shared scope')
    if role is not None and role not in ROLE_PERMISSIONS:raise ValueError('Unknown role')
    if role is None:Membership.objects.filter(scope=scope,user=user).delete()
    else:Membership.objects.update_or_create(scope=scope,user=user,defaults={'role':role})
    Scope.objects.filter(pk=scope.pk).update(acl_revision=F('acl_revision')+1)
    audit(actor,'membership.changed',scope.pk,{'user_id':user.pk,'role':role})


@transaction.atomic
def create_set(user, name, scope_id, key, purpose='normative'):
    if not isinstance(name,str) or not name.strip() or len(name)>160:raise ValueError('Set name required')
    if purpose not in ('normative','experience'):raise ValueError('Set purpose')
    scope=Scope.objects.select_for_update().get(pk=scope_id)
    require(user,scope,'upload')
    # Namespace retries by actor, without revealing another user's idempotency keys.
    request_key=digest(['create-set',user.pk,key])
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    existing=Command.objects.filter(idempotency_key=request_key).first()
    if existing:
        if (existing.normative_set.scope_id!=scope.pk or existing.payload['metadata']['name']!=name.strip()
            or existing.normative_set.purpose!=purpose):raise Conflict('Create retry differs')
        return existing.normative_set
    dataset=NormativeSet.objects.create(scope=scope,name=name.strip(),purpose=purpose,created_by=user)
    payload=dict(set_id=str(dataset.pk),scope_id=str(scope.pk),metadata_revision=1,metadata={'name':dataset.name,'purpose':purpose})
    command(user,dataset,'set.register',request_key,payload)
    audit(user,'set.created',dataset.pk)
    return dataset


@transaction.atomic
def enqueue_preparation(user, set_id, release_id, source_revisions, versions, key):
    """Internal handoff for stage 3/4 ingestion. Not an arbitrary browser command API."""
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    require(user,dataset.scope,'upload')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if not isinstance(source_revisions,list) or not source_revisions or not all(isinstance(x,str) for x in source_revisions) or not isinstance(versions,dict) or not versions:raise ValueError('Sources and versions required')
    payload=dict(set_id=str(dataset.pk),release_id=str(uuid.UUID(str(release_id))),source_revisions=source_revisions,versions=versions)
    return command(user,dataset,'release.prepare',digest(['prepare',user.pk,key]),payload)


@transaction.atomic
def prepare_selected_sources(user,set_id,source_ids,expected_revision,key,mode='complete'):
    from knowledge_v2.ingest import PARSER_VERSION
    from knowledge_v2.norms import EXTRACTOR_VERSION
    from knowledge_v2.quality import MODE,VERSION as QUALITY_VERSION
    if mode not in ('complete',MODE):raise ValueError('Unknown preparation mode')
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    if dataset.purpose!='normative':raise NotReady('Only normative sets contain source requirements')
    if dataset.state=='archived':raise NotReady('Archived set cannot be prepared')
    require(user,dataset.scope,'upload')
    if type(expected_revision) is not int or expected_revision!=dataset.metadata_revision:
        raise Conflict('Set revision changed')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if not isinstance(source_ids,list) or not 1<=len(source_ids)<=100 or len(source_ids)!=len(set(source_ids)):
        raise ValueError('Select unique sources')
    identity=digest(['prepare-selected',user.pk,key])
    old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        if old.normative_set_id!=dataset.pk or old.payload['source_revisions']!=sorted(source_ids) or old.payload['expected_revision']!=expected_revision or old.payload.get('mode','complete')!=mode:
            raise Conflict('Preparation retry differs')
        return old
    sources=list(SourceUpload.objects.filter(normative_set=dataset,pk__in=source_ids))
    if len(sources)!=len(source_ids) or any(x.state not in ('prepared','partial') for x in sources):
        raise NotReady('Sources are not ready')
    extractor_versions=set();analyses={}
    for source in sources:
        last=Command.objects.filter(normative_set=dataset,kind='source.analyze',payload__source_id=str(source.pk)).order_by('-created').first()
        if not last or last.state!='done':
            raise NotReady('Requirements extraction is incomplete')
        summary=last.result.get('summary',{})
        if mode=='complete' and summary.get('semantic_completeness')=='partial':raise NotReady('Semantic analysis has unresolved gaps')
        if mode==MODE:
            if summary.get('extractor_version')!='semantic-9.1.3' or summary.get('analysis_errors')!=0:
                raise NotReady('Для тестового выпуска завершите смысловой анализ без технических ошибок')
            analyses[str(source.pk)]=dict(run_id=summary['run_id'],summary_digest=digest(summary))
        extractor_versions.add(last.payload['extractor_version'])
    if len(extractor_versions)!=1:raise NotReady('Analyze selected sources with the same extractor version')
    if Command.objects.filter(normative_set=dataset,kind__in=['release.prepare','release.publish'],state__in=['pending','delivering']).exists():
        raise Conflict('Preparation or publication is running')
    rid=str(uuid.uuid4())
    versions={'parser':PARSER_VERSION,'extractor':next(iter(extractor_versions))}
    from .curation import selection
    from knowledge_v2.curation import VERSION
    frozen=selection(user,dataset,source_ids)
    versions.update(curation=VERSION,curation_digest=digest(frozen))
    if mode==MODE:versions.update(quality=QUALITY_VERSION,analysis_selection_digest=digest(analyses))
    payload=dict(set_id=str(dataset.pk),actor_id=user.pk,release_id=rid,
                 source_revisions=sorted(source_ids),expected_revision=expected_revision,versions=versions,
                 curation=frozen,previous_release=str(dataset.active_release_id) if dataset.active_release_id else None)
    if mode==MODE:payload.update(mode=mode,analyses=analyses)
    c=command(user,dataset,'release.prepare',identity,payload)
    audit(user,'release.prepare_requested',rid,{'sources':len(source_ids)})
    return c


@transaction.atomic
def publish(user, set_id, release_id, expected_revision, key):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    if dataset.purpose!='normative':raise NotReady('Use experience publication for lessons')
    if dataset.state=='archived':raise NotReady('Restore the set before publication')
    require(user,dataset.scope,'publish')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    request_key=digest(['publish',user.pk,key])
    old=Command.objects.filter(idempotency_key=request_key).first()
    if old:
        if old.normative_set_id!=dataset.pk or old.payload['release_id']!=str(release_id) or old.payload['expected_revision']!=expected_revision:raise Conflict('Publish retry differs')
        return old
    if dataset.metadata_revision!=expected_revision:raise Conflict('Metadata revision changed')
    release=Release.objects.get(pk=release_id,normative_set=dataset)
    if release.state!='ready':raise NotReady('Release is not ready')
    if release.manifest.get('versions',{}).get('curation_digest'):
        from .curation import selection
        selected=[x['id'] for x in release.manifest['items'] if x['kind']=='source_revision']
        if digest(selection(user,dataset,selected))!=release.manifest['versions']['curation_digest']:
            raise Conflict('Карточки или профили изменились. Подготовьте новый выпуск перед публикацией.')
    else:require_integrated_expert_drafts(dataset)
    if Command.objects.filter(normative_set=dataset,kind='release.publish',state__in=['pending','delivering']).exists():raise Conflict('Publication already pending')
    payload=dict(set_id=str(dataset.pk),release_id=str(release.pk),manifest_hash=release.manifest_hash,expected_revision=expected_revision)
    c=command(user,dataset,'release.publish',request_key,payload)
    audit(user,'release.publish_requested',release.pk)
    return c


@transaction.atomic
def change_set_state(user,set_id,action,expected_revision):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    require(user,dataset.scope,'publish')
    if type(expected_revision) is not int or dataset.metadata_revision!=expected_revision:
        raise Conflict('Metadata revision changed')
    if Command.objects.filter(normative_set=dataset,state__in=['pending','delivering']).exclude(kind='review.execute').exists():
        raise NotReady('Wait for active knowledge commands before changing set state')
    if action=='archive' and dataset.state!='archived':dataset.state='archived'
    elif action=='restore' and dataset.state=='archived':
        dataset.state='ready' if dataset.active_release_id else 'empty'
    else:raise Conflict('Set cannot change to requested state')
    dataset.metadata_revision+=1;dataset.save(update_fields=['state','metadata_revision'])
    audit(user,'set.'+action,dataset.pk)
    return dataset


@transaction.atomic
def revoke_release(user,set_id,release_id,reason,expected_revision,key):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    require(user,dataset.scope,'publish')
    if dataset.purpose!='normative' or str(dataset.active_release_id)!=str(release_id):raise NotReady('Only active normative release can be revoked')
    if type(expected_revision) is not int or dataset.metadata_revision!=expected_revision:raise Conflict('Metadata revision changed')
    if not isinstance(reason,str) or not 10<=len(reason.strip())<=1000:raise ValueError('Explain revocation')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if Command.objects.filter(normative_set=dataset,state__in=['pending','delivering']).exclude(kind='review.execute').exists():
        raise NotReady('Wait for active knowledge commands before revocation')
    identity=digest(['release-revoke',user.pk,key]);old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        if old.payload.get('release_id')!=str(release_id) or old.payload.get('reason')!=reason:raise Conflict('Revocation retry differs')
        return old
    payload=dict(set_id=str(dataset.pk),release_id=str(release_id),reason=reason,expected_revision=expected_revision)
    c=command(user,dataset,'release.revoke',identity,payload)
    audit(user,'release.revoke_requested',release_id,{'reason':reason})
    return c


PERMISSION={'trace.suggest':'upload','set.register':'upload','source.ingest':'upload','source.analyze':'upload','release.prepare':'upload','release.publish':'publish','release.revoke':'publish','review.execute':'read','review.submit':'review','review.approve':'publish','review.reject':'publish','review.revoke':'publish','experience.publish':'publish','review.repair':'publish','review.suggest':'review'}


PERMISSION['expert.apply']='read'


@transaction.atomic
def analyze_source(user,set_id,source_id,key):
    source=SourceUpload.objects.select_for_update().select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    if source.normative_set.purpose!='normative':raise NotReady('Source analysis requires normative set')
    if source.normative_set.state=='archived':raise NotReady('Restore the set before analysis')
    require(user,source.normative_set.scope,'upload')
    if source.state not in ('prepared','partial'):raise NotReady('Source structure not ready')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    c=command(user,source.normative_set,'source.analyze',digest(['analyze',user.pk,key]),
              dict(set_id=str(set_id),source_id=str(source_id),sha256=source.sha256,extractor_version='semantic-9.1.3'))
    audit(user,'source.analysis_requested',source.pk)
    return c


@transaction.atomic
def add_analysis(worker_id,command_id,lease,sequence,entries,entry_hash):
    if (type(sequence) is not int or sequence<0 or not isinstance(entries,list) or not 1<=len(entries)<=100
        or any(not isinstance(x,dict) or not {'id','locator','state','validation'}<=x.keys() for x in entries)
        or digest(entries)!=entry_hash):raise ValueError('Invalid analysis chunk')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='source.analyze')
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering'
        or c.lease_until<=timezone.now()):raise Conflict('Stale analysis lease')
    require(c.actor,c.normative_set.scope,'upload')
    chunk,created=AnalysisChunk.objects.get_or_create(command=c,sequence=sequence,defaults={'entries':entries,'digest':entry_hash})
    if not created and chunk.digest!=entry_hash:raise Conflict('Analysis chunk collision')
    return chunk


@transaction.atomic
def claim(worker_id, capabilities, features=()):
    now=timezone.now()
    # Only explicit unified launches depend on the preceding package executor.
    for c in Command.objects.select_for_update().filter(state='delivering',lease_until__lte=now):
        c.state='failed' if c.attempts>=c.max_attempts else 'pending'
        c.lease=None;c.lease_until=None;c.save(update_fields=['state','lease','lease_until'])
        if c.kind=='review.execute' and c.state=='failed':
            KnowledgeCheck.objects.filter(pk=c.payload['job_id']).update(state='failed',summary={'error':'worker_lease_expired'})
            from .launch import reconcile
            reconcile(KnowledgeCheck.objects.get(pk=c.payload['job_id']).batch)
        if c.kind=='source.ingest':
            SourceUpload.objects.filter(pk=c.payload['source_id']).update(state='error' if c.state=='failed' else 'queued',
                result={'reason':'attempts_exhausted'} if c.state=='failed' else {})
    for c in Command.objects.select_for_update().filter(state='pending',kind__in=capabilities).order_by('created','id'):
        if c.kind=='review.execute':
            # Additive rollout: a new worker never takes an old pinned review,
            # and an old worker cannot take a newly planned review.
            if c.payload.get('planning_version') not in (None,'context-budget-v3'):continue
            optimized=c.payload.get('planning_version')=='context-budget-v3'
            if optimized != ('context-budget-v3' in features):continue
        if ('trace.suggest' not in capabilities and
            ((c.kind=='review.execute' and c.payload.get('trace_version')) or
             (c.kind=='release.prepare' and c.payload.get('curation',{}).get('links')))):
            continue  # A previous worker may finish its source analysis, never execute a newer contract.
        if c.attempts>=c.max_attempts or not allowed(c.actor,c.normative_set.scope,PERMISSION.get(c.kind,'')):
            c.state='failed';c.result={'reason':'attempts_exhausted_or_authorization_revoked'};c.save(update_fields=['state','result'])
            if c.kind=='source.ingest':SourceUpload.objects.filter(pk=c.payload['source_id']).update(state='error',result=c.result)
            if c.kind=='review.execute':
                KnowledgeCheck.objects.filter(pk=c.payload['job_id']).update(state='failed',summary=c.result)
                from .launch import reconcile
                reconcile(KnowledgeCheck.objects.get(pk=c.payload['job_id']).batch)
            continue
        if c.kind=='review.execute':
            from .launch import dependency_ready
            if not dependency_ready(c):continue
        c.state='delivering';c.attempts+=1;c.worker_id=worker_id;c.lease=uuid.uuid4();c.lease_until=now+timedelta(seconds=600 if c.kind in ('source.ingest','source.analyze','review.execute') else 120)
        c.save(update_fields=['state','attempts','worker_id','lease','lease_until'])
        if c.kind=='source.ingest':SourceUpload.objects.filter(pk=c.payload['source_id']).update(state='processing')
        return dict(command_id=str(c.pk),kind=c.kind,payload=c.payload,lease=str(c.lease),lease_until=c.lease_until.isoformat())
    return None


@transaction.atomic
def renew(worker_id,command_id,lease):
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id)
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering'
        or not c.lease_until or c.lease_until<=timezone.now()):raise Conflict('Stale worker lease')
    require(c.actor,c.normative_set.scope,PERMISSION.get(c.kind,''))
    c.lease_until=timezone.now()+timedelta(seconds=600 if c.kind in ('source.ingest','source.analyze','review.execute') else 120)
    c.save(update_fields=['lease_until'])
    return c.lease_until


@transaction.atomic
def add_coverage(worker_id,command_id,lease,source_id,sequence,entries,entry_hash):
    if (type(sequence) is not int or sequence<0 or not isinstance(entries,list) or not 1<=len(entries)<=500
        or any(not isinstance(x,dict) or not {'locator','state','reason'}<=x.keys() for x in entries)
        or digest(entries)!=entry_hash):raise ValueError('Invalid coverage chunk')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='source.ingest')
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering'
        or c.lease_until<=timezone.now() or c.payload['source_id']!=str(source_id)):
        raise Conflict('Stale coverage lease')
    require(c.actor,c.normative_set.scope,'upload')
    source=SourceUpload.objects.get(pk=source_id,normative_set=c.normative_set)
    chunk,created=CoverageChunk.objects.get_or_create(source=source,sequence=sequence,
                                                       defaults={'entries':entries,'digest':entry_hash})
    if not created and chunk.digest!=entry_hash:raise Conflict('Coverage chunk collision')
    return chunk


@transaction.atomic
def fail_source(worker_id,command_id,lease,reason,permanent):
    if reason=='trace_suggestion_failed':
        c=Command.objects.select_for_update().select_related('normative_set__scope').get(pk=command_id,kind='trace.suggest')
        if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Stale suggestion lease')
        if type(permanent) is not bool:raise ValueError('Permanent flag')
        require(c.actor,c.normative_set.scope,'upload')
        failed=permanent or c.attempts>=c.max_attempts
        c.state='failed' if failed else 'pending';c.result={'reason':reason,'attempts':c.attempts}
        c.lease=None;c.lease_until=None;c.save(update_fields=['state','result','lease','lease_until'])
        return {'accepted':True,'state':c.state}
    if reason=='expert_validation_failed':
        c=Command.objects.select_for_update().get(pk=command_id,kind='expert.apply')
        if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Stale expert lease')
        c.state='failed';c.result={'reason':reason};c.save(update_fields=['state','result'])
        from .models import ExpertCard
        ExpertCard.objects.filter(pending=c).update(pending=None)
        return {'accepted':True,'state':'failed'}
    if reason not in {'invalid_source','conversion_failed','dependency_unavailable','processing_failed','transfer_failed'}:
        raise ValueError('Failure code')
    if type(permanent) is not bool:raise ValueError('Permanent flag')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind__in=['source.ingest','source.analyze'])
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering'
        or c.lease_until<=timezone.now()):raise Conflict('Stale worker lease')
    require(c.actor,c.normative_set.scope,'upload')
    failed=permanent or c.attempts>=c.max_attempts
    c.state='failed' if failed else 'pending';c.result={'reason':reason,'attempts':c.attempts}
    c.lease=None;c.lease_until=None;c.save(update_fields=['state','result','lease','lease_until'])
    if c.kind=='source.ingest':
        SourceUpload.objects.filter(pk=c.payload['source_id']).update(state='error' if failed else 'queued',result=c.result)
    return {'accepted':True,'state':c.state}


@transaction.atomic
def fail_check(worker_id,command_id,lease,reason,permanent):
    if reason not in {'review_validation_failed','review_execution_failed'} or type(permanent) is not bool:
        raise ValueError('Check failure code')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='review.execute')
    if (c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering'
        or c.lease_until<=timezone.now()):raise Conflict('Stale worker lease')
    require(c.actor,c.normative_set.scope,'read')
    failed=permanent or c.attempts>=c.max_attempts
    c.state='failed' if failed else 'pending';c.result={'reason':reason,'attempts':c.attempts}
    c.lease=None;c.lease_until=None;c.save(update_fields=['state','result','lease','lease_until'])
    if failed:
        KnowledgeCheck.objects.filter(pk=c.payload['job_id']).update(state='failed',summary=c.result)
        from .launch import reconcile
        reconcile(KnowledgeCheck.objects.get(pk=c.payload['job_id']).batch)
    return {'accepted':True,'state':c.state}


@transaction.atomic
def retry_source(user,set_id,source_id):
    source=SourceUpload.objects.select_for_update().select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    require(user,source.normative_set.scope,'upload')
    c=Command.objects.select_for_update().get(kind='source.ingest',payload__source_id=str(source.pk))
    if source.state!='error' or c.state!='failed':raise Conflict('Source is not failed')
    CoverageChunk.objects.filter(source=source).delete()
    c.state='pending';c.attempts=0;c.lease=None;c.lease_until=None;c.result={}
    c.save(update_fields=['state','attempts','lease','lease_until','result'])
    source.state='queued';source.result={};source.save(update_fields=['state','result'])
    audit(user,'source.retry',source.pk)
    return source


@transaction.atomic
def accept_event(worker_id, event_id, command_id, lease, payload, payload_hash):
    if digest(payload)!=payload_hash:raise ValueError('Payload checksum mismatch')
    c=Command.objects.select_for_update().select_related('normative_set__scope','actor').get(pk=command_id)
    # Authorization is checked on duplicates too; a stale worker lease cannot expose new results.
    if c.worker_id!=worker_id or str(c.lease)!=str(lease):raise PermissionDenied('Wrong worker lease')
    require(c.actor,c.normative_set.scope,PERMISSION.get(c.kind,''))
    receipt=Receipt.objects.filter(pk=event_id).first()
    if receipt:
        if receipt.command_id!=c.pk or receipt.digest!=payload_hash:raise Conflict('Event ID collision')
        return receipt.result
    if c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Stale or completed command')
    dataset=NormativeSet.objects.select_for_update().get(pk=c.normative_set_id)
    if payload.get('set_id')!=str(dataset.pk):raise ValueError('Wrong set')
    if c.kind=='expert.apply' and payload.get('kind')=='expert.apply.done':
        from .expert import accept
        result=accept(c,payload)
    elif c.kind=='trace.suggest' and payload.get('kind')=='trace.suggest.done':
        if payload.get('set_id')!=str(dataset.pk) or payload.get('expert_validation') is not False:raise Conflict('Suggestion identity')
        result={'accepted':True,'suggestions':payload['suggestions'],'model':payload['model']}
    elif c.kind=='review.execute' and payload.get('kind')=='review.execute.done':
        from .models import KnowledgeCheck,KnowledgeFindingChunk
        job=KnowledgeCheck.objects.select_for_update().get(pk=c.payload['job_id'])
        if (payload.get('job_id')!=str(job.pk) or payload.get('task_id')!=job.progress.get('task_id')
            or payload.get('state') not in ('paused','completed','partial')
            or type(payload.get('finding_count')) is not int or payload['finding_count']<0):
            raise Conflict('Check result identity or state')
        chunks=list(KnowledgeFindingChunk.objects.filter(job=job).order_by('sequence'))
        if ([x.sequence for x in chunks]!=list(range(len(chunks)))
            or sum(len(x.entries) for x in chunks)!=payload['finding_count']
            or digest([x.digest for x in chunks])!=payload.get('findings_digest')):
            raise NotReady('Finding ledger is incomplete')
        job.state=payload['state'];job.summary={k:v for k,v in payload.items() if k not in ('kind','set_id')}
        job.pause_requested=payload['state']=='paused';job.save(update_fields=['state','summary','pause_requested'])
        from .launch import reconcile
        reconcile(job.batch)
        audit(c.actor,'knowledge_check.'+job.state,job.pk,{'finding_count':payload['finding_count']})
        result={'accepted':True,'job_id':str(job.pk),'state':job.state}
    elif c.kind=='experience.publish' and payload.get('kind')=='experience.publish.done':
        manifest=payload.get('manifest',{});att=payload.get('attestation',{});h=digest(manifest)
        if (payload.get('manifest_hash')!=h or manifest.get('set_id')!=str(dataset.pk)
            or manifest.get('release_id')!=payload.get('release_id') or not manifest.get('items')
            or any(x.get('kind') not in ('review_proposal','review_case','clarification') for x in manifest['items'])
            or any(att.get(k) is not True for k in ('canonical','fts','vector','provenance'))
            or att.get('manifest_hash')!=h or att.get('record_count')!=len(manifest['items'])
            or not manifest.get('generation_id') or not manifest.get('embedding_space')
            or att.get('embedding_space')!=manifest.get('embedding_space')
            or type(att.get('watermark')) is not int or att['watermark']<1
            or len({(x.get('id'),x.get('version')) for x in manifest['items']})!=len(manifest['items'])
            or dataset.metadata_revision!=c.payload['expected_revision']):raise Conflict('Experience publication mismatch')
        release,created=Release.objects.get_or_create(id=payload['release_id'],defaults=dict(normative_set=dataset,manifest=manifest,manifest_hash=h,attestation=att,state='active'))
        if release.manifest_hash!=h or release.normative_set_id!=dataset.pk:raise Conflict('Experience release collision')
        dataset.active_release=release;dataset.metadata_revision+=1;dataset.state='ready';dataset.save(update_fields=['active_release','metadata_revision','state'])
        audit(c.actor,'experience.published',release.pk)
        result={'accepted':True,'release_id':str(release.pk)}
    elif c.kind.startswith('review.') and payload.get('kind')==c.kind+'.done':
        from .models import ExperienceReview
        obj=ExperienceReview.objects.select_for_update().get(pk=c.payload['proposal_id'],normative_set=dataset)
        expected={'review.submit':'pending','review.approve':'approved','review.reject':'rejected','review.revoke':'revoked','review.repair':'corrected_draft','review.suggest':'pending'}[c.kind]
        if payload.get('proposal_id')!=str(obj.pk) or payload.get('state')!=expected:raise Conflict('Review result mismatch')
        obj.state=expected;obj.result=dict(payload,approved_draft=c.payload['draft']) if c.kind=='review.approve' else payload;obj.save(update_fields=['state','result'])
        audit(c.actor,c.kind+'.completed',obj.pk,{'state':expected,'record_id':payload.get('record_id'),'version':payload.get('version')})
        result={'accepted':True,'proposal_id':str(obj.pk)}
    elif c.kind=='set.register' and payload.get('kind')=='set.registered':
        result={'accepted':True}
    elif c.kind=='source.ingest' and payload.get('kind')=='source.ingested':
        source=SourceUpload.objects.select_for_update().get(pk=c.payload['source_id'],normative_set=dataset)
        if (payload.get('source_id')!=str(source.pk) or payload.get('sha256')!=source.sha256
            or payload.get('parser_version')!=c.payload['parser_version']
            or type(payload.get('fragment_count')) is not int or payload['fragment_count']<0
            or not isinstance(payload.get('classification'),dict)
            or not isinstance(payload.get('coverage_summary'),dict)
            or not isinstance(payload.get('issues'),list)
            or type(payload.get('coverage_count')) is not int or payload['coverage_count']<0):
            raise ValueError('Ingestion result mismatch')
        chunks=list(CoverageChunk.objects.filter(source=source).order_by('sequence'))
        if (len(chunks)!=(payload['coverage_count']+499)//500
            or [x.sequence for x in chunks]!=list(range(len(chunks)))
            or sum(len(x.entries) for x in chunks)!=payload['coverage_count']
            or digest([x.digest for x in chunks])!=payload.get('coverage_digest')):
            raise NotReady('Coverage journal incomplete')
        source.state='partial' if payload['coverage_summary'].get('unreadable',0) else 'prepared'
        source.result={k:payload[k] for k in ('parser_version','fragment_count','classification','coverage_summary','coverage_count','coverage_digest','counts','issues','reuse')}
        source.save(update_fields=['state','result'])
        audit(c.actor,'source.ingested',source.pk,{'sha256':source.sha256,'state':source.state})
        result={'accepted':True,'source_id':str(source.pk)}
    elif c.kind=='source.analyze' and payload.get('kind')=='source.analyzed':
        source=SourceUpload.objects.get(pk=c.payload['source_id'],normative_set=dataset)
        if (payload.get('source_id')!=str(source.pk) or payload.get('source_sha256')!=source.sha256
            or payload.get('extractor_version')!=c.payload['extractor_version']
            or type(payload.get('candidate_count')) is not int or payload['candidate_count']<0
            or payload.get('violation_count') is not None):raise ValueError('Analysis result mismatch')
        chunks=list(c.analysis_chunks.order_by('sequence'))
        if ([x.sequence for x in chunks]!=list(range(len(chunks)))
            or sum(len(x.entries) for x in chunks)!=payload['candidate_count']
            or digest([x.digest for x in chunks])!=payload.get('candidate_digest')):
            raise NotReady('Analysis journal incomplete')
        result={'accepted':True,'summary':payload}
        if payload['extractor_version']=='semantic-9.1.3':
            from .profiles import activate_extracted_profiles
            activate_extracted_profiles(c,source,payload.get('profiles',[]))
        from .expert import project
        project(c,source,[entry for chunk in chunks for entry in chunk.entries])
        audit(c.actor,'source.analyzed',source.pk,{'run_id':payload['run_id'],'candidate_count':payload['candidate_count']})
    elif c.kind=='release.prepare' and payload.get('kind')=='release.ready':
        manifest=payload.get('manifest',{});att=payload.get('attestation',{})
        if not isinstance(manifest,dict) or not isinstance(att,dict) or not isinstance(manifest.get('items'),list):raise NotReady('Manifest schema')
        items=manifest['items']
        if (manifest.get('schema')!=1 or any(not isinstance(x,dict) or not {'id','version','kind','digest'}<=x.keys()
            or not isinstance(x['id'],str) or type(x['version']) is not int or x['version']<1
            or not isinstance(x['digest'],str) or len(x['digest'])!=64 for x in items)):
            raise NotReady('Malformed manifest items')
        if len({(x['id'],x['version']) for x in items})!=len(items):raise NotReady('Duplicate manifest items')
        h=digest(manifest)
        if (payload.get('release_id')!=c.payload['release_id'] or manifest.get('release_id')!=c.payload['release_id']
            or manifest.get('set_id')!=str(dataset.pk) or payload.get('manifest_hash')!=h
            or att.get('manifest_hash')!=h or att.get('embedding_space')!=manifest.get('embedding_space')
            or not manifest.get('embedding_space') or not manifest.get('generation_id')
            or not manifest.get('items') or att.get('record_count')!=len(manifest['items'])
            or type(att.get('watermark')) is not int or att['watermark']<1
            or any(att.get(k) is not True for k in ('canonical','fts','vector','provenance'))
            or manifest.get('versions')!=c.payload['versions']):
            raise NotReady('Incomplete readiness attestation')
        source_ids={x['id'] for x in manifest['items'] if x.get('kind')=='source_revision'}
        if source_ids!=set(c.payload['source_revisions']):raise Conflict('Manifest does not contain requested sources')
        material_summary={}
        if c.payload.get('curation'):
            chunks=list(c.analysis_chunks.order_by('sequence'))
            if (payload.get('curation_digest')!=digest(c.payload['curation'])
                or [x.sequence for x in chunks]!=list(range(len(chunks)))
                or sum(len(x.entries) for x in chunks)!=payload.get('material_count')
                or digest([x.digest for x in chunks])!=payload.get('material_digest')):
                raise NotReady('Pinned release material is incomplete')
            material_summary=dict(trust_summary=payload['trust_summary'],material_count=payload['material_count'],
                                  previous_release=c.payload.get('previous_release'))
            if c.payload.get('mode')=='screened_test':
                q=payload.get('quality_summary',{})
                # A completed in-flight command retains its pinned policy across
                # rolling updates. New preparations always pin the current one.
                if q.get('mode')!='screened_test' or not q.get('version') or q['version']!=c.payload['versions'].get('quality') or not q.get('counts',{}).get('ready') or q.get('complete') is not False:raise NotReady('Missing screened release assessment')
                material_summary['quality_summary']=q
        old=Release.objects.filter(pk=c.payload['release_id']).first()
        if old and (old.normative_set_id!=dataset.pk or old.manifest_hash!=h):raise Conflict('Release collision')
        if not old:Release.objects.create(id=c.payload['release_id'],normative_set=dataset,manifest=manifest,manifest_hash=h,attestation=att)
        result={'accepted':True,'release_id':c.payload['release_id'],**material_summary}
    elif c.kind=='release.publish' and payload.get('kind')=='release.published':
        if payload.get('release_id')!=c.payload['release_id'] or payload.get('manifest_hash')!=c.payload['manifest_hash']:raise Conflict('Publish reply differs')
        if dataset.metadata_revision!=c.payload['expected_revision']:raise Conflict('Publication version conflict')
        release=Release.objects.get(pk=c.payload['release_id'],normative_set=dataset,state='ready')
        if dataset.active_release_id and dataset.active_release_id!=release.pk:
            Release.objects.filter(pk=dataset.active_release_id).update(state='superseded')
        dataset.active_release=release;dataset.metadata_revision+=1;dataset.state='ready';dataset.save(update_fields=['active_release','metadata_revision','state'])
        release.state='active';release.save(update_fields=['state'])
        audit(c.actor,'release.published',release.pk)
        result={'accepted':True,'metadata_revision':dataset.metadata_revision}
    elif c.kind=='release.revoke' and payload.get('kind')=='release.revoked':
        if (dataset.metadata_revision!=c.payload['expected_revision'] or payload.get('release_id')!=c.payload['release_id']
            or payload.get('reason')!=c.payload['reason'] or str(dataset.active_release_id)!=c.payload['release_id']):
            raise Conflict('Revocation result differs')
        Release.objects.filter(pk=c.payload['release_id'],normative_set=dataset).update(state='revoked')
        dataset.active_release=None;dataset.metadata_revision+=1;dataset.state='revoked'
        dataset.save(update_fields=['active_release','metadata_revision','state'])
        audit(c.actor,'release.revoked',c.payload['release_id'],{'reason':c.payload['reason']})
        result={'accepted':True,'metadata_revision':dataset.metadata_revision}
    else:raise ValueError('Event does not match command')
    c.state='done';c.result=result;c.save(update_fields=['state','result'])
    Receipt.objects.create(id=event_id,command=c,digest=payload_hash,result=result)
    return result


@transaction.atomic
def create_snapshot(user, job_id, set_ids, versions):
    if not set_ids or not isinstance(versions,dict) or not versions:raise NotReady('Select prepared normative sets')
    selected=[]
    for sid in sorted(set(str(x) for x in set_ids)):
        dataset=NormativeSet.objects.select_for_update().get(pk=sid)
        if dataset.purpose!='normative':raise NotReady('Normative snapshot cannot contain experience-only set')
        require(user,dataset.scope,'read')
        if dataset.state!='ready':raise NotReady('Normative set is not available for new checks')
        r=dataset.active_release
        if not r or r.state!='active':raise NotReady('Set has no active release')
        selected.append(dict(set_id=str(dataset.pk),release_id=str(r.pk),manifest_hash=r.manifest_hash,
                             generation_id=r.manifest['generation_id'],embedding_space=r.manifest['embedding_space']))
    data=dict(schema=1,engine_generation='v2',releases=selected,versions=versions)
    old=Snapshot.objects.filter(job_id=job_id).first()
    if old:
        if old.owner_id!=user.pk or old.digest!=digest(data):raise Conflict('Job snapshot is immutable')
        return old
    return Snapshot.objects.create(job_id=job_id,owner=user,data=data,digest=digest(data))


def read_snapshot(user, snapshot_id):
    s=Snapshot.objects.get(pk=snapshot_id)
    if s.owner_id!=user.pk and not user.is_staff:raise PermissionDenied('Snapshot owner')
    for item in s.data['releases']:
        dataset=NormativeSet.objects.get(pk=item['set_id'])
        require(user,dataset.scope,'read')
    return s
