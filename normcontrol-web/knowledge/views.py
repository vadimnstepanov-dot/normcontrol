import hmac
import json
from functools import wraps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import OperationalError
from django.http import JsonResponse
from django.http import FileResponse
from django.utils import timezone
from pathlib import Path
from django.views.decorators.csrf import csrf_exempt
from . import services as s
from .access import allowed, require
from .models import Scope, NormativeSet, Release, SourceUpload, Command, CoverageChunk, ExperienceReview, KnowledgeCheck
from . import uploads
from .models import DocumentProfile
from . import profiles as profile_service


def strict_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key')
        result[key]=value
    return result


def invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def boundary(methods, worker=False, max_body=2*1024*1024):
    def decorate(fn):
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:return JsonResponse({'error':'method_not_allowed'},status=405)
            if worker:
                expected=settings.KNOWLEDGE_WORKER_TOKEN
                token=request.headers.get('Authorization','').removeprefix('Bearer ')
                if len(expected)<32 or not hmac.compare_digest(expected.encode('utf-8'),token.encode('utf-8')):
                    return JsonResponse({'error':'worker_authorization_required'},status=403)
            elif not request.user.is_authenticated or not request.user.is_active:
                return JsonResponse({'error':'authentication_required'},status=401)
            try:
                if int(request.META.get('CONTENT_LENGTH') or 0)>max_body or len(request.body)>max_body:
                    return JsonResponse({'error':'request_too_large'},status=413)
                request.knowledge_body=json.loads(request.body or b'{}',object_pairs_hook=strict_object,parse_constant=invalid_constant)
                if not isinstance(request.knowledge_body,dict):raise ValueError('Expected object')
                return fn(request,*args,**kwargs)
            except ObjectDoesNotExist:return JsonResponse({'error':'not_found'},status=404)
            except PermissionDenied:return JsonResponse({'error':'access_denied'},status=403)
            except s.Conflict:return JsonResponse({'error':'version_or_idempotency_conflict'},status=409)
            except s.NotReady:return JsonResponse({'error':'knowledge_not_ready'},status=422)
            except (ValueError,TypeError,KeyError,ValidationError):return JsonResponse({'error':'invalid_request'},status=400)
            except OperationalError:return JsonResponse({'error':'temporarily_unavailable'},status=503)
        return csrf_exempt(wrapped) if worker else wrapped
    return decorate


def fields(request, required, optional=()):
    data=request.knowledge_body
    if not set(required)<=data.keys() or not data.keys()<=set(required)|set(optional):raise ValueError('Unexpected fields')
    return data


@boundary({'GET','POST'})
def scopes(request):
    if request.method=='POST':
        d=fields(request,{'name','kind'},{'parent_id'})
        scope=s.create_scope(request.user,d['name'],d['kind'],d.get('parent_id'))
        return JsonResponse({'id':str(scope.pk),'name':scope.name,'kind':scope.kind},status=201)
    return JsonResponse({'scopes':[{'id':str(x.pk),'name':x.name,'kind':x.kind} for x in Scope.objects.all() if allowed(request.user,x,'read')]})


@boundary({'POST'})
def membership(request, scope_id):
    d=fields(request,{'user_id','role'})
    scope=Scope.objects.get(pk=scope_id);require(request.user,scope,'manage')
    user=get_user_model().objects.get(pk=d['user_id'])
    s.set_membership(request.user,scope_id,user,d['role'])
    return JsonResponse({'ok':True})


def set_json(x):
    from .object_control import metadata
    return dict(control=metadata('area',x.pk),id=str(x.pk),name=x.name,purpose=x.purpose,scope_id=str(x.scope_id),state=x.state,
                metadata_revision=x.metadata_revision,active_release=str(x.active_release_id) if x.active_release_id else None)


@boundary({'GET','POST'})
def sets(request):
    if request.method=='POST':
        d=fields(request,{'name','scope_id'},{'purpose'})
        dataset=s.create_set(request.user,d['name'],d['scope_id'],request.headers.get('Idempotency-Key'),d.get('purpose','normative'))
        return JsonResponse(set_json(dataset),status=201)
    rows=[set_json(x) for x in NormativeSet.objects.select_related('scope').order_by('created') if allowed(request.user,x.scope,'read')]
    return JsonResponse({'sets':rows,'empty':not rows,'engine_generation':'v2'})


@boundary({'GET'})
def detail(request, set_id):
    from .curation import summary
    dataset=NormativeSet.objects.get(pk=set_id);require(request.user,dataset.scope,'read')
    result=set_json(dataset)
    result['releases']=[dict(id=str(r.pk),state=r.state,manifest_hash=r.manifest_hash,
                            source_count=sum(x['kind']=='source_revision' for x in r.manifest.get('items',[])),
                            requirement_count=summary(r).get('total',sum(x['kind']=='requirement' for x in r.manifest.get('items',[]))),
                            trust_summary=summary(r),curated=bool(r.manifest.get('versions',{}).get('curation_digest')),
                            created=r.created.isoformat()) for r in dataset.releases.order_by('-created')]
    result['source_count']=dataset.sources.count()
    result['commands']=[dict(id=str(c.pk),kind=c.kind,state=c.state,attempts=c.attempts,
                             result=c.result,release_id=c.payload.get('release_id'))
                        for c in Command.objects.filter(normative_set=dataset,kind__in=['release.prepare','release.publish','release.revoke']).order_by('-created')[:8]]
    return JsonResponse(result)


@boundary({'GET'})
def compare_releases(request,set_id):
    dataset=NormativeSet.objects.select_related('scope').get(pk=set_id)
    require(request.user,dataset.scope,'read')
    old=Release.objects.get(pk=request.GET.get('old'),normative_set=dataset)
    new=Release.objects.get(pk=request.GET.get('new'),normative_set=dataset)
    if old.pk==new.pk:raise ValueError('Choose two distinct releases')
    from .curation import release_command
    if all(r.manifest.get('versions',{}).get('curation_digest') and release_command(r) for r in (old,new)):
        from .curation import release_catalog
        from knowledge_v2.curation import diff
        changes=diff(release_catalog(old),release_catalog(new))
        before_links={x['id']:x for x in release_command(old).payload['curation'].get('links',[])}
        after_links={x['id']:x for x in release_command(new).payload['curation'].get('links',[])}
        trace_changes=[dict(id=identity,kind='added' if identity not in before_links else 'removed' if identity not in after_links else 'changed',
            old=before_links.get(identity),new=after_links.get(identity)) for identity in sorted(set(before_links)|set(after_links))
            if before_links.get(identity)!=after_links.get(identity)]
        result={'old':str(old.pk),'new':str(new.pk),'semantic':True,
            'trace_links':{'count':len(trace_changes),'examples':trace_changes[:30]},
            'note':'Сравниваются смысл, условия, источник и доверие. Сопоставление только по адресу требует проверки.',
          'material_url':f'/normcontol/knowledge/releases/{new.pk}/?kind=diff'}
        for field,kind in [('added','added'),('removed','removed'),('modified','changed')]:
            rows=[x for x in changes if x['kind']==kind]
            result[field]={'count':len(rows),'examples':[dict(kind='requirement',id=(x.get('new') or x['old'])['lineage'],
                name=(x.get('new') or x['old'])['description'],changes=x.get('changes',[]),
                match_requires_review=x.get('match_requires_review',False)) for x in rows[:30]]}
        return JsonResponse(result)
    before={(x['kind'],x['id']):x for x in old.manifest['items']}
    after={(x['kind'],x['id']):x for x in new.manifest['items']}
    names={str(x.pk):x.display_name for x in SourceUpload.objects.filter(normative_set=dataset)}
    def describe(keys):
        return [{'kind':kind,'id':rid,'name':names.get(rid,'')} for kind,rid in sorted(keys)[:30]]
    added=after.keys()-before.keys();removed=before.keys()-after.keys()
    modified={key for key in before.keys()&after.keys() if before[key]['digest']!=after[key]['digest']}
    return JsonResponse({'old':str(old.pk),'new':str(new.pk),
        'added':{'count':len(added),'examples':describe(added)},
        'removed':{'count':len(removed),'examples':describe(removed)},
        'modified':{'count':len(modified),'examples':describe(modified)},
        'note':'Сравнение состава и хешей. Содержательный смысл изменений требует проверки карточек и исходных цитат.'})


def source_json(source):
    from .object_control import metadata
    analysis=Command.objects.filter(normative_set_id=source.normative_set_id,kind='source.analyze',payload__source_id=str(source.pk)).order_by('-created').first()
    from .source_identity import history
    return dict(control=metadata('source',source.pk),id=str(source.pk),set_id=str(source.normative_set_id),name=source.display_name,filename=source.filename,identification=source.identification,identification_revision=source.identification_revision,identity_history=history(source),
        sha256=source.sha256,size=source.size,state=source.state,
        supersedes=str(source.supersedes_id) if source.supersedes_id else None,result=source.result,
        analysis={'state':analysis.state,'summary':analysis.result.get('summary',{})} if analysis else None)


def source_upload(request, set_id):
    """Multipart upload has its own streaming boundary; regular v2 routes are JSON."""
    if request.method!='POST':return JsonResponse({'error':'method_not_allowed'},status=405)
    if not request.user.is_authenticated or not request.user.is_active:return JsonResponse({'error':'authentication_required'},status=401)
    try:
        if not request.content_type.startswith('multipart/form-data'):raise ValueError('Multipart required')
        rows=uploads.upload(request.user,set_id,request.FILES.getlist('files'),request.POST.get('supersedes') or None)
        return JsonResponse({'sources':[dict(**source_json(source),duplicate=duplicate) for source,duplicate in rows]},status=201)
    except ObjectDoesNotExist:return JsonResponse({'error':'not_found'},status=404)
    except PermissionDenied:return JsonResponse({'error':'access_denied'},status=403)
    except s.Conflict:return JsonResponse({'error':'version_or_idempotency_conflict'},status=409)
    except (ValueError,TypeError,ValidationError):return JsonResponse({'error':'invalid_upload'},status=400)
    except OperationalError:return JsonResponse({'error':'temporarily_unavailable'},status=503)


@boundary({'GET'})
def sources(request,set_id):
    dataset=NormativeSet.objects.select_related('scope').get(pk=set_id)
    require(request.user,dataset.scope,'read')
    return JsonResponse({'sources':[source_json(x) for x in dataset.sources.order_by('created','id')]})


@boundary({'GET'})
def source_detail(request,set_id,source_id):
    dataset=NormativeSet.objects.select_related('scope').get(pk=set_id)
    require(request.user,dataset.scope,'read')
    return JsonResponse(source_json(SourceUpload.objects.get(pk=source_id,normative_set=dataset)))


@boundary({'GET'})
def source_coverage(request,set_id,source_id):
    source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    require(request.user,source.normative_set.scope,'read')
    try:page=int(request.GET.get('page','0'))
    except ValueError:raise ValueError('Page number')
    if page<0 or page>100000:raise ValueError('Page number')
    chunk=CoverageChunk.objects.filter(source=source,sequence=page).first()
    return JsonResponse({'source_id':str(source.pk),'page':page,'entries':chunk.entries if chunk else [],
                         'total':source.result.get('coverage_count',0),'page_size':500,
                         'has_next':CoverageChunk.objects.filter(source=source,sequence=page+1).exists()})


def source_download(request,set_id,source_id):
    if request.method!='GET':return JsonResponse({'error':'method_not_allowed'},status=405)
    if not request.user.is_authenticated or not request.user.is_active:return JsonResponse({'error':'authentication_required'},status=401)
    try:
        source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
        require(request.user,source.normative_set.scope,'read')
        return FileResponse((uploads.incoming_directory()/source.storage_key).open('rb'),as_attachment=True,filename=source.filename)
    except ObjectDoesNotExist:return JsonResponse({'error':'not_found'},status=404)
    except PermissionDenied:return JsonResponse({'error':'access_denied'},status=403)
    except OSError:return JsonResponse({'error':'source_unavailable'},status=503)


@boundary({'POST'})
def source_retry(request,set_id,source_id):
    fields(request,set())
    return JsonResponse(source_json(s.retry_source(request.user,set_id,source_id)),status=202)


@boundary({'GET','POST'})
def source_analysis(request,set_id,source_id):
    source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    require(request.user,source.normative_set.scope,'read')
    if request.method=='POST':
        fields(request,set())
        c=s.analyze_source(request.user,set_id,source_id,request.headers.get('Idempotency-Key'))
        return JsonResponse({'command_id':str(c.pk),'state':c.state},status=202)
    c=Command.objects.filter(normative_set_id=set_id,kind='source.analyze',payload__source_id=str(source_id)).order_by('-created').first()
    page=int(request.GET.get('page','0'))
    if page<0 or page>100000:raise ValueError('Page')
    chunk=c.analysis_chunks.filter(sequence=page).first() if c else None
    return JsonResponse({'state':c.state if c else 'not_started','result':c.result if c else {},
        'command_id':str(c.pk) if c else None,'page':page,'entries':chunk.entries if chunk else [],
        'has_next':c.analysis_chunks.filter(sequence=page+1).exists() if c else False})


@boundary({'GET','POST'})
def profiles(request):
    if request.method=='POST':
        d=fields(request,{'scope_id','definition'})
        row=profile_service.save_profile(request.user,d['scope_id'],d['definition'])
        return JsonResponse({'id':str(row.pk),'revision':row.revision},status=201)
    return JsonResponse({'profiles':[dict(id=str(p.pk),scope_id=str(p.scope_id),name=p.name,revision=p.revision,definition=p.definition)
        for p in DocumentProfile.objects.select_related('scope').order_by('name') if allowed(request.user,p.scope,'read')],
        'can_edit':request.user.is_staff})


@boundary({'GET','PUT'})
def profile_detail(request,profile_id):
    row=DocumentProfile.objects.select_related('scope').get(pk=profile_id);require(request.user,row.scope,'read')
    if request.method=='PUT':
        d=fields(request,{'expected_revision','definition'})
        row=profile_service.save_profile(request.user,row.scope_id,d['definition'],row.pk,d['expected_revision'])
    return JsonResponse({'id':str(row.pk),'revision':row.revision,'definition':row.definition,
        'history':[dict(revision=r.revision,digest=r.digest,created=r.created.isoformat()) for r in row.revisions.order_by('-revision')]})


@boundary({'POST'},worker=True)
def worker_analysis(request):
    d=fields(request,{'command_id','lease','sequence','entries','digest'})
    s.add_analysis(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['sequence'],d['entries'],d['digest'])
    return JsonResponse({'accepted':True})


@boundary({'POST'},worker=True)
def worker_release_material(request):
    from .curation import add_material
    d=fields(request,{'command_id','lease','sequence','entries','digest'})
    add_material(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['sequence'],d['entries'],d['digest'])
    return JsonResponse({'accepted':True})


@boundary({'GET'})
def release_material(request,set_id,release_id):
    from .curation import material_page,release_command
    r=Release.objects.select_related('normative_set__scope').get(pk=release_id,normative_set_id=set_id)
    require(request.user,r.normative_set.scope,'read')
    kind=request.GET.get('kind','requirement')
    if kind not in ('requirement','diff'):raise ValueError('Material kind')
    c=release_command(r)
    return JsonResponse(dict(**material_page(r,kind,int(request.GET.get('page','1')),quality=request.GET.get('quality',''),profile=request.GET.get('profile','')),
        summary=c.result if c else {},profiles=c.payload.get('curation',{}).get('profiles',[]) if c else []))


@csrf_exempt
def worker_source(request,command_id,source_id):
    if request.method!='GET':return JsonResponse({'error':'method_not_allowed'},status=405)
    expected=settings.KNOWLEDGE_WORKER_TOKEN
    token=request.headers.get('Authorization','').removeprefix('Bearer ')
    if len(expected)<32 or not hmac.compare_digest(expected.encode(),token.encode()):
        return JsonResponse({'error':'worker_authorization_required'},status=403)
    try:
        c=Command.objects.select_related('actor','normative_set__scope').get(pk=command_id,kind='source.ingest')
        source=SourceUpload.objects.get(pk=source_id,normative_set=c.normative_set)
        if (c.payload.get('source_id')!=str(source.pk) or c.worker_id!=settings.KNOWLEDGE_WORKER_ID
            or c.state!='delivering' or c.lease_until<=timezone.now()
            or request.headers.get('X-Knowledge-Lease')!=str(c.lease)
            or not allowed(c.actor,c.normative_set.scope,'upload')):
            return JsonResponse({'error':'access_denied'},status=403)
        path=uploads.incoming_directory()/source.storage_key
        return FileResponse(path.open('rb'),content_type='application/octet-stream',as_attachment=True,
                            filename=source.filename)
    except ObjectDoesNotExist:return JsonResponse({'error':'not_found'},status=404)
    except OSError:return JsonResponse({'error':'source_unavailable'},status=503)


@boundary({'POST'})
def publish(request, set_id):
    d=fields(request,{'release_id','expected_revision'})
    if type(d['expected_revision']) is not int:raise ValueError('Revision must be an integer')
    c=s.publish(request.user,set_id,d['release_id'],d['expected_revision'],request.headers.get('Idempotency-Key'))
    return JsonResponse({'command_id':str(c.pk),'state':c.state},status=202)


@boundary({'POST'})
def prepare_release(request,set_id):
    d=fields(request,{'source_ids','expected_revision'},{'mode'})
    c=s.prepare_selected_sources(request.user,set_id,d['source_ids'],d['expected_revision'],request.headers.get('Idempotency-Key'),mode=d.get('mode','complete'))
    return JsonResponse({'command_id':str(c.pk),'release_id':c.payload['release_id'],'state':c.state},status=202)


@boundary({'POST'})
def set_state(request,set_id):
    d=fields(request,{'action','expected_revision'})
    dataset=s.change_set_state(request.user,set_id,d['action'],d['expected_revision'])
    return JsonResponse({'id':str(dataset.pk),'state':dataset.state,'metadata_revision':dataset.metadata_revision})


@boundary({'POST'})
def release_revoke(request,set_id):
    d=fields(request,{'release_id','reason','expected_revision'})
    c=s.revoke_release(request.user,set_id,d['release_id'],d['reason'],d['expected_revision'],request.headers.get('Idempotency-Key'))
    return JsonResponse({'command_id':str(c.pk),'state':c.state},status=202)


@boundary({'GET'})
def command_detail(request,command_id):
    c=Command.objects.select_related('normative_set__scope').get(pk=command_id)
    require(request.user,c.normative_set.scope,'read')
    if c.actor_id!=request.user.pk and not allowed(request.user,c.normative_set.scope,'manage'):
        raise PermissionDenied('Command author or manager required')
    return JsonResponse({'id':str(c.pk),'kind':c.kind,'state':c.state,'attempts':c.attempts,
                         'max_attempts':c.max_attempts,'result':c.result,'created':c.created.isoformat()})


@boundary({'POST'})
def snapshots(request):
    d=fields(request,{'job_id','set_ids'})
    if not isinstance(d['set_ids'],list) or len(d['set_ids'])>100:raise ValueError('Set list')
    snap=s.create_snapshot(request.user,d['job_id'],d['set_ids'],settings.KNOWLEDGE_SNAPSHOT_VERSIONS)
    return JsonResponse({'id':str(snap.pk),'digest':snap.digest,'data':snap.data},status=201)


@boundary({'GET'})
def snapshot(request, snapshot_id):
    snap=s.read_snapshot(request.user,snapshot_id)
    return JsonResponse({'id':str(snap.pk),'digest':snap.digest,'data':snap.data})


@boundary({'POST'},worker=True)
def worker_authorize(request):
    data=fields(request,{'user_id','set_id','action'})
    if data['action'] not in ('read','upload','review','publish') or type(data['user_id']) is not int:
        raise ValueError('Unsupported authorization action')
    user=get_user_model().objects.filter(pk=data['user_id'],is_active=True).first()
    dataset=NormativeSet.objects.select_related('scope').filter(pk=data['set_id']).first()
    action='upload' if dataset and dataset.automatic and data['action']=='publish' else data['action']
    return JsonResponse({'allowed':bool(dataset and allowed(user,dataset.scope,action))})


@boundary({'POST'},worker=True)
def worker_claim(request):
    d=fields(request,{'protocol_version','capabilities'},{'features'})
    if d['protocol_version']!=2 or not isinstance(d['capabilities'],list) or not all(isinstance(x,str) for x in d['capabilities']):raise ValueError('Protocol')
    if not set(d['capabilities'])<=s.PERMISSION.keys():raise ValueError('Capabilities')
    features=d.get('features',[])
    if not isinstance(features,list) or any(x not in ('context-budget-v3','context-budget-v4','check-log-v1','visual-tail-v1') for x in features):raise ValueError('Worker features')
    c=s.claim(settings.KNOWLEDGE_WORKER_ID,d['capabilities'],features)
    return JsonResponse({'command':c})


@boundary({'POST'},worker=True,max_body=16*1024*1024)
def worker_event(request):
    d=fields(request,{'event_id','command_id','lease','payload','payload_hash'})
    if not isinstance(d['payload'],dict) or not isinstance(d['payload_hash'],str):raise ValueError('Event shape')
    result=s.accept_event(settings.KNOWLEDGE_WORKER_ID,d['event_id'],d['command_id'],d['lease'],d['payload'],d['payload_hash'])
    return JsonResponse(result)


@boundary({'POST'},worker=True)
def worker_renew(request):
    d=fields(request,{'command_id','lease'})
    until=s.renew(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'])
    return JsonResponse({'lease_until':until.isoformat()})


@boundary({'POST'},worker=True)
def worker_coverage(request):
    d=fields(request,{'command_id','lease','source_id','sequence','entries','digest'})
    s.add_coverage(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['source_id'],
                   d['sequence'],d['entries'],d['digest'])
    return JsonResponse({'accepted':True})


@boundary({'POST'},worker=True)
def worker_fail(request):
    d=fields(request,{'command_id','lease','reason','permanent'})
    if d['reason'] in {'preparation_validation_failed','preparation_failed'}:
        from .curation import fail_preparation
        return JsonResponse(fail_preparation(settings.KNOWLEDGE_WORKER_ID,**d))
    if d['reason'] in {'review_validation_failed','review_execution_failed'}:
        return JsonResponse(s.fail_check(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['reason'],d['permanent']))
    return JsonResponse(s.fail_source(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],
                                      d['reason'],d['permanent']))


@boundary({'POST'})
def review_submit(request,set_id):
    from . import reviews
    obj,c=reviews.submit(request.user,set_id,request.knowledge_body,request.headers.get('Idempotency-Key'))
    return JsonResponse({'id':str(obj.pk),'state':obj.state,'command_id':str(c.pk)},status=202)


@boundary({'GET'})
def review_detail(request,review_id):
    from . import reviews
    obj=reviews.read(request.user,review_id)
    return JsonResponse({'id':str(obj.pk),'revision':obj.revision,'state':obj.state,'submission':obj.submission,'result':obj.result})


@boundary({'GET'})
def review_list(request,set_id):
    dataset=NormativeSet.objects.select_related('scope').get(pk=set_id)
    require(request.user,dataset.scope,'read')
    query=ExperienceReview.objects.filter(normative_set=dataset).order_by('-created')
    if not allowed(request.user,dataset.scope,'publish'):query=query.filter(author=request.user)
    state=request.GET.get('state')
    if state:
        if state not in ('submitted','pending','decision_pending','approved','revoked','rejected','corrected_draft'):
            raise ValueError('Unknown review state')
        query=query.filter(state=state)
    try:page=int(request.GET.get('page','0'))
    except ValueError:raise ValueError('Page')
    if page<0 or page>100000:raise ValueError('Page')
    rows=query[page*30:(page+1)*30+1]
    return JsonResponse({'reviews':[dict(id=str(x.pk),revision=x.revision,state=x.state,created=x.created.isoformat(),
        author=x.author.username if allowed(request.user,dataset.scope,'publish') else 'Вы',
        kind=x.submission['proposal_kind'],comment=x.submission['comment'][:300],
        summary=x.result.get('approved_draft',x.submission['draft']).get('summary','')) for x in rows[:30]],
        'page':page,'has_next':len(rows)>30})


@boundary({'POST'})
def review_decide(request,review_id,action):
    from . import reviews
    obj,c=reviews.decide(request.user,review_id,action,request.knowledge_body,request.headers.get('Idempotency-Key'))
    return JsonResponse({'id':str(obj.pk),'revision':obj.revision,'state':obj.state,'command_id':str(c.pk)},status=202)


@boundary({'POST'})
def experience_publish(request,set_id):
    from . import reviews
    data=fields(request,{'expected_revision'})
    c=reviews.publish(request.user,set_id,data['expected_revision'],request.headers.get('Idempotency-Key'))
    return JsonResponse({'command_id':str(c.pk),'state':c.state},status=202)


@boundary({'GET','POST'})
def checks(request):
    from . import checks as check_service
    if request.method=='POST':
        d=fields(request,{'batch_id','set_ids'},{'experience_set_id'})
        job,c=check_service.start(request.user,d['batch_id'],d['set_ids'],d.get('experience_set_id'),request.headers.get('Idempotency-Key'))
        return JsonResponse({'id':str(job.pk),'state':job.state,'command_id':str(c.pk)},status=202)
    from portal.access import visible_batches
    ids=visible_batches(request.user).values_list('pk',flat=True)
    rows=[]
    for job in KnowledgeCheck.objects.select_related('batch','snapshot','experience_release__normative_set__scope').filter(batch_id__in=ids).order_by('-created')[:50]:
        try:check_service.visible(request.user,job)
        except PermissionDenied:continue
        rows.append({'id':str(job.pk),'batch_id':str(job.batch_id),'batch_name':job.batch.name,'state':job.state,
                     'progress':job.progress,'created':job.created.isoformat()})
    return JsonResponse({'checks':rows})


@boundary({'GET','POST'})
def check_detail(request,job_id):
    from . import checks as check_service
    job=KnowledgeCheck.objects.select_related('batch','snapshot','experience_release__normative_set__scope').get(pk=job_id)
    check_service.visible(request.user,job)
    if request.method=='POST':
        d=fields(request,{'action'})
        job=check_service.control(request.user,job_id,d['action'])
    releases=[dict(item,scope_id=str(NormativeSet.objects.get(pk=item['set_id']).scope_id))
              for item in job.snapshot.data['releases']]
    return JsonResponse({'id':str(job.pk),'batch_id':str(job.batch_id),'state':job.state,
        'progress':job.progress,'summary':job.summary,'pause_requested':job.pause_requested,
        'releases':releases,
        'experience_release':str(job.experience_release_id) if job.experience_release_id else None})


@boundary({'GET'})
def check_findings(request,job_id):
    from . import checks as check_service
    job=KnowledgeCheck.objects.select_related('batch','snapshot','experience_release__normative_set__scope').get(pk=job_id)
    check_service.visible(request.user,job)
    try:page=int(request.GET.get('page','0'))
    except ValueError:raise ValueError('Page')
    if page<0 or page>100000:raise ValueError('Page')
    row=job.finding_chunks.filter(sequence=page).first()
    return JsonResponse({'entries':row.entries if row else [],'page':page,
        'has_next':job.finding_chunks.filter(sequence=page+1).exists()})


@csrf_exempt
def worker_check_file(request,command_id,job_id,document_id):
    if request.method!='GET':return JsonResponse({'error':'method_not_allowed'},status=405)
    expected=settings.KNOWLEDGE_WORKER_TOKEN;token=request.headers.get('Authorization','').removeprefix('Bearer ')
    if len(expected)<32 or not hmac.compare_digest(expected.encode(),token.encode()):return JsonResponse({'error':'worker_authorization_required'},status=403)
    try:
        from portal.models import Document
        from portal.access import visible_batches
        c=Command.objects.select_related('actor','normative_set__scope').get(pk=command_id,kind='review.execute')
        if (c.payload['job_id']!=str(job_id) or c.worker_id!=settings.KNOWLEDGE_WORKER_ID or c.state!='delivering'
            or c.lease_until<=timezone.now() or request.headers.get('X-Knowledge-Lease')!=str(c.lease)
            or not allowed(c.actor,c.normative_set.scope,'read')):return JsonResponse({'error':'access_denied'},status=403)
        if (not visible_batches(c.actor).filter(pk=c.payload['batch_id']).exists()
            or any(not allowed(c.actor,NormativeSet.objects.get(pk=item['set_id']).scope,'read')
                   for item in c.payload['snapshot']['releases'])
            or (c.payload.get('experience_set_id') and
                not allowed(c.actor,NormativeSet.objects.get(pk=c.payload['experience_set_id']).scope,'read'))):
            return JsonResponse({'error':'access_denied'},status=403)
        doc=Document.objects.get(pk=document_id,batch_id=c.payload['batch_id'])
        declared=next((x for x in c.payload['documents'] if x['id']==doc.pk),None)
        if not declared or declared['sha256']!=doc.sha256:return JsonResponse({'error':'access_denied'},status=403)
        return FileResponse(doc.file.open('rb'),as_attachment=True,filename=doc.name)
    except ObjectDoesNotExist:return JsonResponse({'error':'not_found'},status=404)
    except OSError:return JsonResponse({'error':'source_unavailable'},status=503)


@boundary({'POST'},worker=True)
def worker_check_progress(request):
    from . import checks as check_service
    d=fields(request,{'command_id','lease','job_id','progress'})
    return JsonResponse(check_service.progress(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['job_id'],d['progress']))


@boundary({'POST'},worker=True)
def worker_check_findings(request):
    from . import checks as check_service
    d=fields(request,{'command_id','lease','job_id','sequence','entries','digest'})
    return JsonResponse(check_service.add_findings(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['job_id'],d['sequence'],d['entries'],d['digest']))
