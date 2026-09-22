"""Outbound-only local worker bridge. Public clients never submit filesystem paths."""
import hmac
import json
import os
from functools import wraps
from django.db import transaction
from django.http import JsonResponse,FileResponse,HttpResponse
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404,render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.core.paginator import Paginator
from .models import Batch,Document,WorkerRun,ReviewFeedback,FindingDisposition,LLMConfig,WorkerPresence

def worker(view):
    @csrf_exempt
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        expected=os.getenv('NORMCONTROL_WORKER_TOKEN','')
        supplied=request.headers.get('Authorization','').removeprefix('Bearer ')
        if len(expected)<32 or not hmac.compare_digest(expected,supplied):return JsonResponse({'error':'Worker authorization required'},status=403)
        if len(request.body)>30*1024**2:return JsonResponse({'error':'Result too large'},status=413)
        try:return view(request,*args,**kwargs)
        except (ValueError,KeyError,TypeError):return JsonResponse({'error':'Invalid request'},status=400)
    return wrapped

def body(request):return json.loads(request.body or b'{}')

@worker
@require_POST
def ping(request):
    data=body(request);state=data.get('state','idle')
    if state not in ('idle','busy','paused','error'):raise ValueError('state')
    raw=data.get('rag') if isinstance(data.get('rag'),dict) else {}
    settings={k:raw.get('settings',{}).get(k) for k in ('requirements_per_group','evidence_chars_per_group','reference_group_size','verification_group_size','search_normalization')}
    sources=[]
    for item in raw.get('sources',[])[:30] if isinstance(raw.get('sources'),list) else []:
        if isinstance(item,dict):sources.append({k:(str(item.get(k,''))[:240] if k in ('name','sha256') else int(item.get(k,0) or 0)) for k in ('name','sha256','blocks','tables','warnings')})
    rag={'catalog':str(raw.get('catalog',''))[:64],'requirements':int(raw.get('requirements',0) or 0),'contract_version':raw.get('contract_version'),
         'unresolved_dependencies':int(raw.get('unresolved_dependencies',0) or 0),'settings':settings,'sources':sources} if raw else {}
    WorkerPresence.objects.update_or_create(name=str(data.get('worker','local'))[:100],defaults={'state':state,'details':{'rag':rag}})
    return JsonResponse({'ok':True})

@worker
def configuration(request):
    from .llm_connection import settings_payload
    c=LLMConfig.objects.first()
    if not c:return JsonResponse({'error':'Настройте LLM в кабинете администратора'},status=409)
    return JsonResponse(settings_payload(c,True))

@worker
@require_POST
def claim(request):
    data=body(request);name=str(data.get('worker','local'))[:100]
    with transaction.atomic():
        run=WorkerRun.objects.select_for_update().filter(worker=name,state='claimed',local_id='').select_related('batch').first()
        batch=run.batch if run else Batch.objects.select_for_update().filter(status='waiting',worker_run__isnull=True,archived=False).order_by('queue_position','created','pk').first()
        if not batch:return JsonResponse({'job':None})
        if not run:run=WorkerRun.objects.create(batch=batch,worker=name)
        return JsonResponse({'job':str(batch.pk),'lease':str(run.lease),'owner':str(batch.owner_id),'checks':batch.checks,'fresh_review':batch.fresh_review,'files':[{'id':d.id,'name':d.name,'sha256':d.sha256,'size':d.size} for d in batch.documents.all()]})

@worker
@require_POST
def pending_feedback(request):
    name=str(body(request).get('worker','local'))[:100]
    rows=ReviewFeedback.objects.filter(state='pending',batch__worker_run__worker=name,batch__worker_run__state__in=('completed','partial')).select_related('batch__worker_run').order_by('created')[:10]
    return JsonResponse({'feedback':[{'id':f.id,'local_id':f.batch.worker_run.local_id,'lease':str(f.batch.worker_run.lease),'finding_id':f.finding_id,'comment':f.comment} for f in rows]})

@worker
def file(request,lease,pk):
    run=get_object_or_404(WorkerRun,lease=lease);doc=get_object_or_404(Document,batch=run.batch,pk=pk)
    kind='application/msword' if doc.name.casefold().endswith('.doc') else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    return FileResponse(doc.file.open('rb'),content_type=kind)

@worker
@require_POST
def update(request,lease):
    data=body(request)
    with transaction.atomic():
        run=get_object_or_404(WorkerRun.objects.select_for_update().defer('report'),lease=lease)
        seq=int(data['sequence'])
        if seq>run.sequence:
            state=data.get('state','running')
            if state not in ('preparing','running','paused','partial','completed','failed','cancelled'):raise ValueError('state')
            last_frontend_wake=(run.snapshot or {}).get('_frontend_wake_at')
            snapshot=data.get('snapshot',{})
            if last_frontend_wake:snapshot['_frontend_wake_at']=last_frontend_wake
            run.sequence=seq;run.state=state;run.snapshot=snapshot;run.local_id=str(data.get('local_id',''))[:64]
            changed=['sequence','state','snapshot','local_id','control','heartbeat']
            if 'report' in data:
                run.report=data['report'];changed.append('report')
            if (run.control=='pause' and state=='paused') or (run.control=='resume' and state in ('running','preparing')):run.control=''
            run.save(update_fields=changed)
            if run.batch.status!='cancelled':Batch.objects.filter(pk=run.batch_id).update(status=state)
        wake_at=(run.snapshot or {}).get('_frontend_wake_at',0)
        wake=run.state in ('preparing','running') and isinstance(wake_at,(int,float)) and timezone.now().timestamp()-wake_at<720
        return JsonResponse({'accepted_sequence':run.sequence,'cancel':run.batch.status=='cancelled','control':run.control,'wake':wake,'feedback':[{'id':f.id,'finding_id':f.finding_id,'comment':f.comment} for f in run.batch.review_feedback.filter(state='pending')]})

@worker
@require_POST
def feedback_result(request,lease,pk):
    run=get_object_or_404(WorkerRun,lease=lease);f=get_object_or_404(ReviewFeedback,batch=run.batch,pk=pk);data=body(request)
    if data.get('state') not in ('confirmed','rejected','question'):raise ValueError('state')
    f.state=data['state'];f.decision=data.get('decision',{});f.save();return JsonResponse({'ok':True})

def owned(request,pk):
    qs=Batch.objects.all() if request.user.is_staff else Batch.objects.filter(owner=request.user)
    return get_object_or_404(qs,pk=pk)

@login_required
def status(request,pk):
    batch=owned(request,pk)
    include_findings=request.GET.get('include_findings')=='1'
    try:run=WorkerRun.objects.get(batch=batch) if include_findings else WorkerRun.objects.defer('report').get(batch=batch)
    except WorkerRun.DoesNotExist:return JsonResponse({'state':'waiting','snapshot':{},'report':{}})
    result={'state':run.state,'snapshot':run.snapshot,'report_available':bool(run.local_id),'heartbeat':run.heartbeat.isoformat(),'stale':(timezone.now()-run.heartbeat).total_seconds()>120,
        'dispositions':{x.finding_id:{'state':x.state,'comment':x.comment,'author':x.author.get_full_name() or x.author.username,'updated':x.updated.isoformat()} for x in batch.finding_dispositions.select_related('author')}}
    if include_findings:result['findings_preview']=run.report.get('findings',[])[:60]
    return JsonResponse(result)

@login_required
@require_POST
def wake(request,pk):
    batch=owned(request,pk)
    with transaction.atomic():
        run=get_object_or_404(WorkerRun.objects.select_for_update(),batch=batch)
        if run.state not in ('preparing','running'):return JsonResponse({'active':False})
        snapshot=dict(run.snapshot or {});snapshot['_frontend_wake_at']=timezone.now().timestamp()
        run.snapshot=snapshot;run.save(update_fields=['snapshot'])
    return JsonResponse({'active':True,'signalled':True})

@login_required
def report(request,pk):
    batch=owned(request,pk);run=get_object_or_404(WorkerRun,batch=batch)
    if request.GET.get('format')=='json':return JsonResponse(run.report,json_dumps_params={'ensure_ascii':False})
    if request.GET.get('format')=='md':
        r=HttpResponse(run.report.get('markdown_export','Отчёт ещё готовится'),content_type='text/markdown; charset=utf-8');r['Content-Disposition']='attachment; filename="normcontrol-report.md"';return r
    selected=request.GET.get('status','confirmed')
    if selected not in ('confirmed','candidate','question','style','rejected','all'):selected='confirmed'
    decisions={x.finding_id:x for x in batch.finding_dispositions.select_related('author')}
    all_findings=[{**f,'human_disposition':({'state':decisions[f.get('id')].state,'label':decisions[f.get('id')].get_state_display(),'comment':decisions[f.get('id')].comment,'author':decisions[f.get('id')].author.get_full_name() or decisions[f.get('id')].author.username,'updated':decisions[f.get('id')].updated} if f.get('id') in decisions else None)} for f in run.report.get('findings',[])]
    documents=run.report.get('documents',[])
    doc_filter=request.GET.get('document','')[:64];category=request.GET.get('category','')[:80]
    severity=request.GET.get('severity','')[:20];query=request.GET.get('q','').strip()[:160]
    categories=sorted({f.get('category','') for f in all_findings}-{''})
    findings=[f for f in all_findings if selected=='all' or f['status']==selected or selected=='candidate' and f['status']=='verifying']
    if doc_filter:findings=[f for f in findings if any(e.get('document')==doc_filter for e in f.get('evidence',[]))]
    if category:findings=[f for f in findings if f.get('category')==category]
    if severity:findings=[f for f in findings if f.get('severity')==severity]
    if query:
        needle=query.casefold()
        findings=[f for f in findings if needle in (' '.join(str(f.get(k,'')) for k in ('issue','explanation','suggestion'))+' '+' '.join(e.get('address','')+' '+e.get('quote','') for e in f.get('evidence',[]))).casefold()]
    page=Paginator(findings,50).get_page(request.GET.get('page'))
    params=request.GET.copy();params.pop('page',None);params.pop('format',None)
    limitations=[x if isinstance(x,str) else x.get('reason') or x.get('error') or json.dumps(x,ensure_ascii=False) for x in run.report.get('limitations',[])]
    return render(request,'review_report.html',{'batch':batch,'report':run.report,'findings':page,'report_limitations':limitations,'selected':selected,'page':'reports',
        'finding_page':page,'filter_query':params.urlencode(),'filter_documents':documents,'filter_categories':categories,
        'filter_document':doc_filter,'filter_category':category,'filter_severity':severity,'filter_text':query})

@login_required
@require_POST
def feedback(request,pk):
    batch=owned(request,pk);data=body(request);text=str(data.get('comment',''));fid=str(data.get('finding_id',''))
    if not 8<=len(text)<=6000:return JsonResponse({'error':'Комментарий: от 8 до 6000 символов'},status=400)
    run=get_object_or_404(WorkerRun,batch=batch)
    if fid not in {f['id'] for f in run.report.get('findings',[])}:return JsonResponse({'error':'Замечание не найдено'},status=404)
    f=ReviewFeedback.objects.create(batch=batch,author=request.user,finding_id=fid,comment=text);return JsonResponse({'id':f.id})

@login_required
@require_POST
def disposition(request,pk,finding_id):
    batch=owned(request,pk);data=body(request);state=str(data.get('state',''));comment=str(data.get('comment','')).strip()[:6000]
    allowed=dict(FindingDisposition.STATES)
    if state not in allowed:return JsonResponse({'error':'Неизвестный статус'},status=400)
    run=get_object_or_404(WorkerRun,batch=batch)
    if finding_id not in {str(f.get('id','')) for f in run.report.get('findings',[])}:return JsonResponse({'error':'Замечание не найдено'},status=404)
    if state=='disputed' and len(comment)<8:return JsonResponse({'error':'Для несогласия укажите обоснование'},status=400)
    value,_=FindingDisposition.objects.update_or_create(batch=batch,finding_id=finding_id,defaults={'author':request.user,'state':state,'comment':comment})
    return JsonResponse({'state':value.state,'label':allowed[value.state],'author':request.user.get_full_name() or request.user.username,'updated':value.updated.isoformat()})
