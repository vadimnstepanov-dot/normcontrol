from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,render,redirect
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from types import SimpleNamespace
from portal.access import visible_batches,can_edit
from .access import allowed
from .models import NormativeSet,KnowledgeCheck
from .checks import visible

def decision_explanation(decision):
    reason=decision.get('reason','')
    translated={
        'No complete verified decision':'Недостаточно доказательств для итогового вывода.',
        'Positive evidence verified across all partitions':'Выполнение подтверждено по всем проверенным частям.',
        'Absence verified across every planned text partition':'Отсутствие подтверждено по всем запланированным частям.'}
    parts=[translated.get(reason,reason)]
    parts.extend(p['reason'] for p in decision.get('partition_decisions',[]) if p.get('reason'))
    return '\n\n'.join(dict.fromkeys(parts))


@login_required
def start(request,batch_id):
    from .launch import LaunchForm,start as launch
    from .services import Conflict,NotReady
    batch=get_object_or_404(visible_batches(request.user).prefetch_related('documents'),pk=batch_id)
    manageable=can_edit(request.user,batch)
    active=KnowledgeCheck.objects.filter(batch=batch,state__in=['queued','running','paused']).first()
    if active:return redirect('knowledge-check-monitor',job_id=active.pk)
    form=LaunchForm(request.POST or None,user=request.user,batch=batch)
    from portal.intake import form_context,launch_response
    run=getattr(batch,'worker_run',None)
    if run:
        form.initial['checks']=list(dict.fromkeys([*batch.checks,'sto']))
    if request.method=='POST':
        if not manageable:raise PermissionDenied('Batch owner required')
        if form.is_valid():
            try:
                data=form.cleaned_data
                job=launch(request.user,batch.pk,data['checks'],data.get('normative_sets',[]),data.get('experience'),data['launch_key'])
                return launch_response(request,batch)
            except (ValueError,Conflict,NotReady) as e:form.add_error(None,str(e))
        if request.headers.get('X-Requested-With')=='XMLHttpRequest':
            from django.http import JsonResponse
            return JsonResponse({'errors':form.errors.get_json_data()},status=422)
    return render(request,'launch.html',{'page':'batches','batch':batch,'form':form,
        'can_manage':manageable,'attaching':bool(run),**form_context(form)})


def _can_see(user,job):
    try:visible(user,job);return True
    except Exception:return False


@login_required
def monitor(request,job_id):
    job=get_object_or_404(KnowledgeCheck.objects.select_related('batch','snapshot','experience_release__normative_set__scope'),pk=job_id)
    visible(request.user,job)
    kind=request.GET.get('format')
    if kind:
        if kind not in ('xlsx','docx'):return HttpResponse(status=400)
        if job.state not in ('completed','partial'):return HttpResponse('Отчёт ещё не завершён',status=409)
        from portal.report_export import make_xlsx,make_docx
        documents=[{'id':d.sha256,'name':d.name} for d in job.batch.documents.all()]
        findings=[]
        for chunk in job.finding_chunks.order_by('sequence'):
            for decision in chunk.entries:
                if decision.get('category')=='traceability':
                    link=decision['link'];cites=link.get('basis_citations',[])
                    findings.append(dict(id=decision['id'],status={'violated':'confirmed','checked':'checked','not_applicable':'not_applicable'}.get(decision['state'],'candidate' if decision.get('preliminary_violation') else 'question'),
                        category='Междокументная логика',issue=link['description'],explanation=decision['reason'],
                        evidence=[dict(e,address=e.get('location') or e.get('locator')) for e in decision.get('evidence',[])],
                        suggestion='',source=dict(document_name=link.get('basis_name',''),clause='; '.join(c.get('locator','') for c in cites),source_quote='\n'.join(c['quote'] for c in cites))))
                    continue
                obligation=decision['obligation'];citation=obligation.get('atom',{}).get('citation',{})
                evidence=[dict(e,address=e.get('location') or e.get('locator'))
                    for part in decision.get('partition_decisions',[]) for e in part.get('evidence',[])]
                visual=decision.get('visual_evidence')
                visual_note=('\nИзображение (предварительно): '+visual.get('observation','')+'; вхождения: '+', '.join(visual.get('occurrences',[]))+'; область: '+str(visual.get('bbox',[]))) if visual else ''
                findings.append(dict(id=obligation['id'],status={'violated':'confirmed','checked':'checked','not_applicable':'not_applicable'}.get(decision['state'],'candidate' if decision.get('preliminary_violation') else 'question'),
                    category='СТО',issue=f"Требование {citation.get('locator','—')}: {obligation.get('atom',{}).get('description') or obligation.get('atom',{}).get('object','')}",
                    explanation=decision_explanation(decision)+visual_note,evidence=evidence,suggestion='',
                    source={'document_name':obligation.get('source',{}).get('filename',''),
                            'clause':citation.get('locator',''),'source_quote':citation.get('quote','')}))
        run=SimpleNamespace(report={'documents':documents},snapshot={})
        batch=SimpleNamespace(name=job.batch.name,get_status_display=lambda:'Завершена' if job.state=='completed' else 'С ограничениями')
        trace_errors=job.summary.get('traceability',{}).get('errors',{})
        errors=[dict(id=k,stage='vision' if k.startswith('visual:') else 'links' if k.removeprefix('trace:') in trace_errors else 'sto',stage_label='Изображения' if k.startswith('visual:') else 'Междокументная логика' if k.removeprefix('trace:') in trace_errors else 'Требования СТО',state='failed',attempts=1,error=v.get('error','Причина неизвестна'))
                for k,v in job.summary.get('errors',{}).items()]
        limitations=job.summary.get('limitations',[])
        selected='; '.join(f"{r['set_id']} / выпуск {r['release_id']}" for r in job.snapshot.data['releases'])
        content=make_xlsx(batch,run,findings,errors,limitations,selected) if kind=='xlsx' else make_docx(batch,run,findings,errors,limitations,selected)
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if kind=='xlsx' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        response=HttpResponse(content,content_type=mime)
        response['Content-Disposition']=f'attachment; filename="normcontrol-v2-{str(job.pk)[:8]}.{kind}"'
        return response
    return render(request,'knowledge_check_monitor.html',{'page':'batches','job':job,
        'can_manage':can_edit(request.user,job.batch)})
