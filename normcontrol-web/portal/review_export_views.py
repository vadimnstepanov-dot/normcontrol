"""Authorized, persisted snapshot-based export; independent of register pagination."""
import json,os,time,tempfile,uuid
from collections import Counter
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.files.base import ContentFile
from django.http import JsonResponse,FileResponse,HttpResponse
from django.shortcuts import render,get_object_or_404,redirect
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from .access import visible_batch,editable_batch,can_edit
from .models import WorkerRun,Document,WordReviewExport,WordReviewProposal
from .word_review import plan,generate,sha,fingerprint,ReviewError,VERSION
class WaitingForMemory(ReviewError):pass
STATE_LABELS={'running':'Проверяется','preparing':'Подготовка','queued':'В очереди','paused':'Приостановлена','completed':'Завершена','partial':'Завершена с ограничениями','failed':'Ошибка','cancelled':'Отменена'}

def capture(user,batch,*,include_resolved=False):
 run=WorkerRun.objects.filter(batch=batch).first();findings=list((run.report or {}).get('findings',[])) if run else [];states={}
 if run:states['native']={'state':run.state,'label':run.report.get('status') or STATE_LABELS.get(run.state,'Состояние не определено'),'sequence':run.sequence,'updated':run.report.get('updated'),'metrics':run.report.get('metrics',{}),'documents':run.report.get('documents',[]),'tasks':dict(Counter(t.get('state','unknown') for t in run.report.get('tasks',[]))),'limitations':len(run.report.get('limitations',[]))}
 if settings.KNOWLEDGE_V2_ENABLED:
  from knowledge.models import KnowledgeCheck
  from knowledge.checks import visible
  for job in KnowledgeCheck.objects.filter(batch=batch).select_related('batch','snapshot'):
   try:visible(user,job)
   except PermissionDenied:continue
   states[str(job.pk)]={'state':job.state,'label':STATE_LABELS.get(job.state,'Состояние не определено'),'progress':job.progress,'summary':job.summary}
   for chunk in job.finding_chunks.order_by('sequence'):
    for d in chunk.entries:
     if d.get('state') in ('checked','not_applicable') and not include_resolved:continue
     if d.get('category')=='traceability':item=d.get('link',{});fid=d.get('id');issue=item.get('description','Связь документов')
     else:item=d.get('obligation',{});fid=item.get('id');issue=item.get('atom',{}).get('description','Нормативное требование')
     evidence=list(d.get('evidence',[]))+[e for p in d.get('partition_decisions',[]) for e in p.get('evidence',[])]
     if not fid:continue
     if d.get('category')=='traceability':
      citations=item.get('basis_citations',[]);source={'document_name':item.get('basis_name',''),'clause':'; '.join(c.get('locator','') for c in citations),'source_quote':'\n'.join(c.get('quote','') for c in citations),'citations':citations}
     else:
      citation=item.get('atom',{}).get('citation',{});origin=item.get('source',{})
      source={'document_name':origin.get('filename',''),'clause':citation.get('locator',''),'source_quote':citation.get('quote',''),'source_sha256':citation.get('source_sha256') or origin.get('sha256',''),'citation':citation}
     findings.append({'id':str(fid),'status':d['state'] if d.get('state') in ('checked','not_applicable') else 'confirmed' if d.get('state')=='violated' and not d.get('preliminary_violation') else 'candidate' if d.get('preliminary_violation') else 'question',
      'issue':issue,'explanation':d.get('reason',''),'suggestion':'Уточнить выполнение требования; сравнить с сохранённым нормативным основанием.', 'evidence':evidence,'source':source,'_origin':'normative'})
 decisions={d.finding_id:{'state':d.state,'comment':d.comment,'updated':d.updated.isoformat()} for d in batch.finding_dispositions.all()}
 data={'states':states,'findings':findings,'decisions':decisions}
 identity={**data,'states':{k:{field:value for field,value in state.items() if field!='sequence'} for k,state in states.items()}}
 return data,fingerprint(identity)

def source_document(document,snapshot):
 source=Path(document.file.path)
 if sha(source)!=document.sha256:raise ReviewError('Изменён исходный документ '+document.name)
 with source.open('rb') as handle:original_docx=handle.read(4)==b'PK\x03\x04'
 working=source if original_docx else Path(document.review_working_file.path) if document.review_working_file else None
 if working is None:raise ReviewError('Для DOC нужна точная рабочая DOCX-копия обработчика. Она ещё не передана: '+document.name)
 working_hash=sha(working)
 if not original_docx and working_hash!=document.review_working_sha256:raise ReviewError('Изменилась рабочая DOCX-копия '+document.name)
 aliases={document.sha256,document.sha256[:20]}
 for d in snapshot['states'].get('native',{}).get('documents',[]):
  if d.get('sha256')==working_hash:aliases.add(str(d['id']))
 # The source hash and canonical hash are explicit; never associate by filename.
 return source,working,{'id':document.pk,'source_sha256':document.sha256,'working_sha256':working_hash,'aliases':sorted(aliases),'role':document.review_role,'name':document.name}

def make_plans(batch,snapshot,version,ids,preliminary=False,style=False):
 targets=list(batch.documents.filter(pk__in=ids,review_role='target').order_by('id'))
 if len(targets)!=len(set(ids)):raise ReviewError('Выберите только проверяемые документы с явно заданной ролью')
 references=set()
 reference_versions=[]
 for d in batch.documents.filter(review_role='approved_reference'):
  if sha(d.file.path)!=d.sha256:raise ReviewError('Изменён утверждённый эталон '+d.name)
  reference_versions.append({'id':d.pk,'sha256':d.sha256})
  references.update((d.sha256,d.sha256[:20]))
  if d.review_working_file:
   _,_,description=source_document(d,snapshot)
   references.update(description['aliases'])
 plans=[]
 for d in targets:
  source,working,description=source_document(d,snapshot);description['reference_aliases']=sorted(references);description['references']=reference_versions
  proposals={p.finding_id:p.operation for p in WordReviewProposal.objects.filter(batch=batch,document=d,result_version=version).order_by('created')}
  plans.append(plan(source,working,description,version,snapshot['findings'],snapshot['decisions'],proposals,preliminary,style))
 return plans

@login_required
def workspace(request,pk):
 batch=visible_batch(request.user,pk);error='';record=None;preview=[]
 try:
  if request.method=='POST':
   batch=editable_batch(request.user,pk)
   if request.POST.get('action')=='roles':
    with transaction.atomic():
     for d in batch.documents.select_for_update():
      role=request.POST.get('role_'+str(d.pk),'unassigned')
      if role not in ('target','approved_reference','unassigned'):raise ReviewError('Неизвестная роль документа')
      pinned=batch.review_scope.get(str(d.pk))
      if pinned in ('target','approved_reference') and role!=pinned:
       raise ReviewError('Роли закреплены при запуске проверки. Для изменения области создайте новую проверку.')
      d.review_role=role;d.save(update_fields=['review_role'])
    return redirect('word-review',pk=pk)
   snapshot,version=capture(request.user,batch)
   if request.POST.get('action')=='proposal':
    d=get_object_or_404(Document,batch=batch,pk=request.POST.get('document'),review_role='target')
    source,working,description=source_document(d,snapshot)
    f=next((f for f in snapshot['findings'] if str(f['id'])==request.POST.get('finding') and f.get('status')=='confirmed'),None)
    if not f:raise ReviewError('Нужно подтверждённое замечание из текущего снимка')
    draft=plan(source,working,description,version,[f],snapshot['decisions'])
    options=[o for o in draft['operations'] if o['reason']!='Повторяющаяся цитата: точный диапазон неоднозначен' and o['validation']=='comment_only']
    if len(options)!=1:raise ReviewError('Для редакции нужен единственный подтверждённый диапазон замечания')
    operation={k:options[0][k] for k in ('document_id','result_version','source_sha256','working_sha256','part','locator','start','end','original')}
    kind=request.POST.get('kind','replace');proposed=request.POST.get('replacement','')
    if kind not in ('replace','insert','delete') or not request.POST.get('confirmed'):raise ReviewError('Подтвердите правильность конкретной редакции')
    operation.update(type=kind,proposed=proposed,verification='specialist_confirmed',basis='Подтверждённая редакция специалиста')
    WordReviewProposal.objects.create(batch=batch,document=d,finding_id=f['id'],result_version=version,operation=operation,author=request.user)
    return redirect('word-review',pk=pk)
   ids=[int(x) for x in request.POST.getlist('documents')];preliminary=request.POST.get('preliminary')=='on';style=request.POST.get('style')=='on'
   if not ids:raise ReviewError('Выберите проверяемый документ')
   plans=make_plans(batch,snapshot,version,ids,preliminary,style)
   key=fingerprint({'version':version,'generator':VERSION,'plans':[{k:v for k,v in p.items() if k!='created'} for p in plans]})
   with transaction.atomic():
    # Serialize through the batch; changes are stored in a frozen snapshot first.
    from .models import Batch
    Batch.objects.select_for_update().get(pk=batch.pk)
    record,_=WordReviewExport.objects.get_or_create(batch=batch,key=key,defaults={'owner':request.user,'snapshot':snapshot,'plans':plans})
   return redirect(reverse('word-review',kwargs={'pk':pk})+'?export='+str(record.pk))
  if request.GET.get('export'):
   record=get_object_or_404(WordReviewExport,batch=batch,pk=request.GET['export'])
  else:
   snapshot,version=capture(request.user,batch)
   for d in batch.documents.filter(review_role='target'):
    try:preview.extend(make_plans(batch,snapshot,version,[d.pk]))
    except ReviewError as e:error+=str(e)+'; '
 except (ReviewError,ValueError) as e:error=str(e)
 data,version=capture(request.user,batch)
 coverage=(record.snapshot if record else data)['states']
 incomplete=not coverage or any(s.get('state')!='completed' for s in coverage.values())
 context={'batch':batch,'documents':list(batch.documents.order_by('id')),'error':error,'record':record,'preview':preview,'result_version':version,
  'incomplete':incomplete,'states':coverage,'snapshot_findings':len((record.snapshot if record else data)['findings']),'can_manage':can_edit(request.user,batch),'findings':[f for f in data['findings'] if f.get('status')=='confirmed'],'page':'reports'}
 if request.GET.get('format')=='json':return JsonResponse({'version':version,'incomplete':incomplete,'preview':preview,'export':str(record.pk) if record else None,'state':record.state if record else None,'plans':record.plans if record else [],'result':record.result if record else {},'error':error},json_dumps_params={'ensure_ascii':False},status=409 if error else 200)
 return render(request,'word_review.html',context)

@login_required
def artifact(request,pk,export_id):
 batch=visible_batch(request.user,pk)
 try:
  with transaction.atomic():
   record=get_object_or_404(WordReviewExport.objects.select_for_update(),batch=batch,pk=export_id)
   if request.method=='POST' and record.state not in ('ready','building'):
    editable_batch(request.user,pk);record.state='building';record.error='';record.save(update_fields=['state','error']);build=True
   else:build=False
  if request.method=='POST' and record.state=='ready':return redirect(reverse('word-review',kwargs={'pk':pk})+'?export='+str(record.pk))
  if build:
   if getattr(settings,'WORD_REVIEW_BACKGROUND',True):
    record.state='queued';record.save(update_fields=['state'])
    from .review_export_queue import kick
    kick()
   else:build_export(record)
   return redirect(reverse('word-review',kwargs={'pk':pk})+'?export='+str(record.pk))
  if request.method=='GET' and record.state=='ready':
   # Cached content is versioned, but a changed original invalidates download too.
   for p in record.plans:
    for reference in p['document'].get('references',[]):
     ref=get_object_or_404(Document,batch=batch,pk=reference['id'])
     if ref.review_role!='approved_reference' or sha(ref.file.path)!=reference['sha256']:raise ReviewError('Версия или роль утверждённого эталона изменилась')
    d=get_object_or_404(Document,batch=batch,pk=p['document']['id']);source,working,_=source_document(d,record.snapshot)
    if d.review_role!='target' or sha(source)!=p['source_sha256'] or sha(working)!=p['working_sha256']:raise ReviewError('Исходник, рабочая копия или роль изменились; сохранённый экспорт не выдаётся')
   return FileResponse(record.artifact.open('rb'),as_attachment=True,filename=Path(record.artifact.name).name)
  return HttpResponse('Экспорт ещё не сформирован',status=409)
 except Exception as e:
  from django.http import Http404
  from django.core.exceptions import PermissionDenied
  if isinstance(e,(Http404,PermissionDenied)):raise
  if 'record' in locals() and locals().get('build'):record.state='failed';record.error=str(e);record.save(update_fields=['state','error'])
  return HttpResponse(str(e),status=409)

@login_required
def application_report(request,pk,export_id):
 batch=visible_batch(request.user,pk);record=get_object_or_404(WordReviewExport,batch=batch,pk=export_id)
 return JsonResponse({'state':record.state,'error':record.error,'key':record.key,'plans':record.plans,'result':record.result},json_dumps_params={'ensure_ascii':False})


def build_export(record):
 batch=record.batch
 started=time.monotonic();results=[]
 with tempfile.TemporaryDirectory(prefix='normcontrol-word-') as folder:
  outputs=[]
  for index,p in enumerate(record.plans):
   for reference in p['document'].get('references',[]):
    ref=get_object_or_404(Document,batch=batch,pk=reference['id'])
    if ref.review_role!='approved_reference' or sha(ref.file.path)!=reference['sha256']:raise ReviewError('Версия или роль утверждённого эталона изменилась')
   d=get_object_or_404(Document,batch=batch,pk=p['document']['id'])
   if d.review_role!='target':raise ReviewError('Роль документа изменена: эталон не аннотируется')
   source,working,_=source_document(d,record.snapshot);out=Path(folder)/('review-'+str(index)+'.docx')
   if os.name=='posix' and Path('/proc/meminfo').exists():
    available=next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
    with ZipFile(working) as z:estimated=96*1024**2+22*z.getinfo('word/document.xml').file_size
    if available<estimated+128*1024**2:raise WaitingForMemory('Недостаточно RAM сервера для экспорта; файл не опубликован. Повторите формирование после освобождения памяти.')
   result=generate(source,working,p,out);result['document_id']=d.pk;result['filename']=Path(d.name).stem+' — правки.docx';results.append(result);outputs.append((result['filename'],out))
  combined={'documents':results,'seconds':round(time.monotonic()-started,3),'version':record.key,'snapshot_version':record.plans[0]['result_version'],
   'edits':sum(r['edits'] for r in results),'comments':sum(r['comments'] for r in results),'skips':sum(r['skips'] for r in results)}
  filename='normcontrol-review-'+str(record.pk)[:8]
  if len(outputs)==1:payload=outputs[0][1].read_bytes();filename+='.docx'
  else:
   bundle=Path(folder)/'review.zip'
   with ZipFile(bundle,'w',ZIP_DEFLATED) as z:
    for i,(name,path) in enumerate(outputs):z.write(path,str(i+1)+' — '+name)
    z.writestr('Применение.json',json.dumps(combined,ensure_ascii=False,indent=2))
   payload=bundle.read_bytes();filename+='.zip'
  record.artifact.save(filename,ContentFile(payload),save=False);record.result=combined;record.state='ready';record.save()
