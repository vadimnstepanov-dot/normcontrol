from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from .views import boundary,fields
from .access import allowed,require
from .models import NormativeSet,NormativeLink,KnowledgeCheck
from .checks import visible
from . import trace_links


@login_required
def workspace(request):
    return render(request,'knowledge_trace.html',dict(page='knowledge'))


@boundary({'GET','POST'})
def links(request):
    dataset=NormativeSet.objects.select_related('scope').get(pk=request.GET.get('set_id') or request.knowledge_body.get('set_id'))
    require(request.user,dataset.scope,'read')
    if request.method=='POST':
        d=fields(request,{'set_id','source','target','basis','source_type','target_type','relation','description','condition',
                         'mandatory_target','confidence','status','reason'})
        row=trace_links.save(request.user,d['set_id'],d,key=request.headers.get('Idempotency-Key'))
        from .automatic import advance
        advance(dataset,request.user)
        return JsonResponse(row.payload,status=201)
    return JsonResponse(dict(entries=[dict(x.payload,control=__import__('knowledge.object_control',fromlist=['metadata']).metadata('link',x.pk)) for x in dataset.trace_links.order_by('id')],
        can_edit=allowed(request.user,dataset.scope,'upload'),can_confirm=allowed(request.user,dataset.scope,'publish')))


@boundary({'GET','POST'})
def link(request,identity):
    row=NormativeLink.objects.select_related('normative_set__scope').get(pk=identity)
    require(request.user,row.normative_set.scope,'read')
    if request.method=='POST':
        d=fields(request,{'expected_revision','source','target','basis','source_type','target_type','relation','description','condition',
                         'mandatory_target','confidence','status','reason'})
        saved=trace_links.save(request.user,row.normative_set_id,d,identity,key=request.headers.get('Idempotency-Key'))
        from .automatic import advance
        advance(row.normative_set,request.user)
        return JsonResponse(saved.payload)
    return JsonResponse(dict(row.payload,history=[dict(revision=h.revision,payload=h.payload,created=h.created.isoformat())
        for h in row.history.order_by('-revision')[:30]]))


@boundary({'POST'})
def suggest(request):
    d=fields(request,{'set_id','cards'})
    c=trace_links.suggest(request.user,d['set_id'],d['cards'],request.headers.get('Idempotency-Key'))
    return JsonResponse(dict(command_id=str(c.pk),state=c.state),status=202)


@boundary({'GET'})
def matrix(request,job_id):
    job=KnowledgeCheck.objects.select_related('batch','snapshot','experience_release__normative_set__scope').get(pk=job_id)
    visible(request.user,job)
    rows=[row for chunk in job.finding_chunks.order_by('sequence') for row in chunk.entries if row.get('category')=='traceability']
    return JsonResponse(dict(state=job.state,rows=rows,summary=job.summary.get('traceability',{})))
