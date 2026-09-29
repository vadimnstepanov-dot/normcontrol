import io
import mimetypes
from pathlib import PurePosixPath
from zipfile import ZipFile,BadZipFile
from django.contrib.auth.decorators import login_required
from django.db.models import Q,Count
from django.http import JsonResponse,HttpResponse
from django.shortcuts import render
from .views import boundary,fields
from .access import require,allowed
from .models import Scope,ExpertCard,ExpertCardRevision,DocumentProfile,SourceUpload,Command
from . import expert,uploads


@login_required
def workspace(request):return render(request,'knowledge_expert.html',{'page':'rag'})


@login_required
def release_workspace(request,release_id):
    from django.shortcuts import get_object_or_404
    from .models import Release
    r=get_object_or_404(Release.objects.select_related('normative_set__scope'),pk=release_id)
    require(request.user,r.normative_set.scope,'read')
    return render(request,'knowledge_release.html',dict(page='rag',release=r,dataset=r.normative_set))


@boundary({'GET'})
def catalog(request):
    scopes=[s for s in Scope.objects.all() if allowed(request.user,s,'read')]
    return JsonResponse(dict(scopes=[dict(id=str(s.pk),name=s.name) for s in scopes],
        profiles=[dict(id=str(p.pk),name=p.name,scope_id=str(p.scope_id),parents=p.definition.get('parents',[]),
            glossary='глоссар' in p.name.lower()) for p in DocumentProfile.objects.filter(scope__in=scopes,archived=False).order_by('name')]))


def profile_for(request,scope):
    return DocumentProfile.objects.get(pk=request.GET['profile'],scope=scope) if request.GET.get('profile') else None


@boundary({'GET'})
def cards(request):
    scope=Scope.objects.get(pk=request.GET['scope']);require(request.user,scope,'read');profile=profile_for(request,scope)
    query=ExpertCard.objects.filter(source__normative_set__scope=scope,latest_analysis=True).select_related('source__normative_set','pending')
    if request.GET.get('set_id'):query=query.filter(source__normative_set_id=request.GET['set_id'])
    if profile:
        expression=Q(pk__in=[])
        for sid,pid in expert.bindings(profile):expression|=Q(source_id=sid,profile_key=pid)
        from .curation import inventory
        from knowledge_v2.curation import profile_graph
        # Only local refinements of this profile/ancestors belong in this view.
        profiles=[dict(id=str(p.pk),definition=p.definition,revision=p.revision) for p in DocumentProfile.objects.filter(scope=scope,archived=False)]
        _,closure=profile_graph(profiles)
        query=query.filter(expression|Q(payload__local_profile__in=list(closure[str(profile.pk)])))
    if request.GET.get('type'):query=query.filter(entity_type=request.GET['type'])
    if request.GET.get('glossary')=='1':query=query.filter(entity_type='definition')
    if request.GET.get('status'):query=query.filter(status=request.GET['status'])
    else:query=query.exclude(status__in=['rejected','superseded'])
    if request.GET.get('q'):
        q=request.GET['q'][:300];query=query.filter(Q(description__icontains=q)|Q(source__filename__icontains=q)|Q(source__identification__fields__short_title__value__icontains=q)|Q(source__identification__fields__full_title__value__icontains=q)|Q(section__icontains=q))
    sections=list(query.order_by().values('section').annotate(count=Count('id')).order_by('section')[:250])
    if request.GET.get('section'):query=query.filter(section=request.GET['section'])
    page=int(request.GET.get('page','1'));size=min(50,max(1,int(request.GET.get('size','25'))))
    if not 1<=page<=100000:raise ValueError('Page')
    count=query.count()
    rows=query.defer('payload','contexts').order_by('source__filename','section','id')[(page-1)*size:page*size]
    analyses=Command.objects.filter(normative_set__scope=scope,kind='source.analyze').order_by('-created')[:10]
    return JsonResponse(dict(total=count,page=page,pages=max(1,(count+size-1)//size),entries=[expert.card_json(c,profile=profile) for c in rows],
        sections=sections,
        analyses=[dict(state=c.state,source_id=c.payload.get('source_id'),partial=c.result.get('summary',{}).get('semantic_completeness')=='partial') for c in analyses]))


@boundary({'GET','POST'})
def card(request,identity):
    row=ExpertCard.objects.select_related('source__normative_set__scope','analysis').get(pk=identity)
    require(request.user,row.source.normative_set.scope,'read')
    if request.method=='POST':
        d=fields(request,{'action','expected_revision'},{'profile_id','reason','patch','parts','others','description','acknowledge_questions','split_logic',
            'resolution_reason','review_import_completeness','refinement_kind','condition','basis_index'})
        c=expert.submit(request.user,identity,d,request.headers.get('Idempotency-Key'))
        return JsonResponse(dict(command_id=str(c.pk),state=c.state),status=202)
    profile=profile_for(request,row.source.normative_set.scope)
    value=expert.card_json(row,True,profile,request.user)
    if request.GET.get('revision'):
        revision=ExpertCardRevision.objects.get(card=row,revision=int(request.GET['revision']))
        value.update(payload=revision.payload,revision=revision.revision,description=revision.payload.get('description',''),
            status=revision.payload.get('expert_status','unreviewed'),can_edit=False,can_refine=False,historical=True,
            publication='historical',trust={},quality=None,quality_reasons=[])
    return JsonResponse(value)


@boundary({'GET'})
def revision(request,identity,number):
    row=ExpertCard.objects.select_related('source__normative_set__scope').get(pk=identity)
    require(request.user,row.source.normative_set.scope,'read')
    h=ExpertCardRevision.objects.get(card=row,revision=number)
    return JsonResponse(dict(revision=h.revision,payload=h.payload,reason=h.reason,action=h.action))


@boundary({'GET'})
def history(request,identity):
    row=ExpertCard.objects.select_related('source__normative_set__scope').get(pk=identity)
    require(request.user,row.source.normative_set.scope,'read')
    page=int(request.GET.get('page','1'))
    if not 1<=page<=100000:raise ValueError('History page')
    entries=row.history.select_related('actor').order_by('-revision')[(page-1)*30:page*30]
    return JsonResponse(dict(has_next=row.history.count()>page*30,entries=[dict(revision=h.revision,action=h.action,reason=h.reason,
        created=h.created.isoformat(),author=h.actor.get_username() if h.actor else 'Модель') for h in entries]))


@boundary({'GET'})
def images(request,source_id):
    """Serve only embedded raster originals, never active SVG/HTML/OLE content."""
    s=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id)
    require(request.user,s.normative_set.scope,'read')
    if not s.filename.lower().endswith('.docx'):return JsonResponse({'entries':[]})
    try:
        with ZipFile(uploads.incoming_directory()/s.storage_key) as archive:
            names=[x for x in archive.infolist() if x.filename.startswith('word/media/') and '..' not in PurePosixPath(x.filename).parts
                and x.file_size<=12*1024*1024 and PurePosixPath(x.filename).suffix.lower() in ('.png','.jpg','.jpeg','.gif','.webp')]
            if 'image' not in request.GET:
                return JsonResponse({'entries':[dict(name=PurePosixPath(x.filename).name,key=x.filename,size=x.file_size) for x in names[:500]]})
            selected=next((x for x in names if x.filename==request.GET['image']),None)
            if not selected:raise ValueError('Image not in source')
            raw=archive.read(selected)
            if not (raw.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF87a',b'GIF89a')) or (raw[:4]==b'RIFF' and raw[8:12]==b'WEBP')):raise ValueError('Not a raster image')
            response=HttpResponse(raw,content_type=mimetypes.guess_type(selected.filename)[0] or 'image/png')
            response['Cache-Control']='private, no-store';response['X-Content-Type-Options']='nosniff';return response
    except (OSError,BadZipFile):return JsonResponse({'error':'source_unavailable'},status=503)
