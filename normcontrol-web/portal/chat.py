"""Small persisted control surface over canonical intake, controls and exports.

No model inference, user supplied paths, command names or remote URLs here.
"""
import json,re,uuid,math
from datetime import datetime,timezone as dt_timezone
from functools import wraps
from collections import Counter
from types import SimpleNamespace
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse,HttpResponse
from django.shortcuts import get_object_or_404,render,redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone
from .models import AccessProfile,Batch,Conversation,ChatMessage,WorkerRun,WordReviewExport
from .access import visible_batch,can_edit
from .intake import UploadForm,launch_uploaded,form_context,DEFAULT_REVIEW_PROMPT,review_prompt
from .forms import CHECKS

MAX_TEXT=6000
PROGRESS_KEY=uuid.UUID('80881fbb-95eb-409a-b969-920dd5a98486')

def checked_input(view):
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        try:return view(request,*args,**kwargs)
        except (json.JSONDecodeError,TypeError,KeyError,AttributeError):
            return JsonResponse({'error':'Некорректные параметры запроса.'},status=400)
    return wrapped

def launch_form(user,data=None):
    if settings.KNOWLEDGE_V2_ENABLED:
        from knowledge.launch import NewLaunchForm
        return NewLaunchForm(data,user=user)
    return UploadForm(data)

def choices(user):
    form=launch_form(user)
    return {'checks':[{'id':k,'name':v} for k,v in CHECKS],
            'norms':form_context(form)['norms'],
            'experiences':[{'id':k,'name':v.name} for k,v in getattr(form,'experiences',{}).items()]}

def owned(user,cid):return get_object_or_404(Conversation,pk=cid,owner=user)

def intent(text):
    value=text.casefold().strip()
    if not re.search(r'\b(проверь\w*|проверить|запусти\w*|начни)\b',value):
        return 'details' if re.search(r'замечани|подробн|реестр',value) else 'status'
    explicit=[]
    for key,pattern in [('sto',r'\bсто\b|норматив'),('logic',r'логик'),('language',r'граммат|грамот|язык|терминолог|орфограф'),('formatting',r'оформлен')]:
        if re.search(pattern,value):explicit.append(key)
    return explicit or ['sto','logic','language']

def review_clock(batch,run):
    """Wall time, independent of browser lifetime; never imply GPU activity."""
    now=timezone.now().timestamp()
    def stamp(value):
        try:
            if isinstance(value,datetime):return value.timestamp()
            if isinstance(value,str):
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=dt_timezone.utc)
                return parsed.timestamp()
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0:return float(value)
        except (ValueError,TypeError,OverflowError):pass
        return None
    starts=[];ends=[]
    if run:
        for data in (run.snapshot,run.report):
            if not isinstance(data,dict):continue
            if value:=stamp(data.get('created')):starts.append(value)
            if value:=stamp(data.get('updated')):ends.append(value)
        if value:=stamp(run.heartbeat):ends.append(value)
    if settings.KNOWLEDGE_V2_ENABLED:
        for job in batch.knowledge_checks.all():
            if value:=stamp(job.progress.get('updated_at')):ends.append(value)
    queued=batch.status=='waiting'
    live=batch.status in ('waiting','preparing','running')
    started=min(starts) if starts else batch.created.timestamp()
    if queued:started=batch.created.timestamp()
    # Older jobs have no package-finished timestamp. Show the last recorded
    # update explicitly rather than inventing an end time when polling resumes.
    end=now if live else min(now,max(ends)) if ends else None
    elapsed=max(0,(end if end is not None else started)-started)
    label=('В очереди уже' if queued else 'Работает уже' if live and starts else 'С момента запуска' if live else
           'Приостановлено; прошло' if batch.status=='paused' else 'До последнего обновления')
    return {'seconds':int(elapsed),'live':live,'label':label,'started_at':started,
            'known':live or bool(ends),'note':'Время с начала обработки, включая паузы и ожидание этапов.' if starts else 'Время с момента отправки проверки.'}

def compact(user,conversation):
    batch=conversation.batch
    if not batch:return None
    visible_batch(user,batch.pk)
    from .review_export_views import capture
    snapshot,version=capture(user,batch)
    counts=Counter(f.get('status') for f in snapshot['findings'])
    states=snapshot['states'];complete=batch.status=='completed' and bool(states) and all(s.get('state')=='completed' for s in states.values())
    terminal=batch.status in ('completed','partial','failed','cancelled')
    text=('Проверка завершена полностью.' if complete else 'Проверка завершена с ограничениями.' if batch.status in ('completed','partial') else batch.get_status_display()+'.')
    text+=f"\nПодтверждённых ошибок: {counts['confirmed']}; вопросов: {counts['question']+counts['candidate']+counts['verifying']}."
    if not complete:text+='\nПолное соответствие не установлено; результат может быть неполным.' if terminal else '\nИтоговые выводы ещё не сформированы.'
    elif 'sto' not in batch.checks:text+='\nСТО не оценивалось в этом запуске.'
    if terminal:text+='\nФайлы формируются по сохранённому результату.'
    actions=[]
    if can_edit(user,batch):
        if batch.status in ('waiting','running','preparing'):actions=['pause','cancel'] if batch.status!='waiting' else ['cancel']
        elif batch.status=='paused':actions=['resume','cancel']
        elif terminal:actions=['rerun']
    progress=None
    # Only factual planned task totals, never estimated stage percentages.
    run=WorkerRun.objects.filter(batch=batch).first()
    native=run.snapshot if run else {}
    stages=native.get('stages',{})
    total=sum(sum(int(n) for n in s.values() if isinstance(n,int)) for s in stages.values() if isinstance(s,dict))
    done=sum(int(s.get('done',0))+int(s.get('failed',0)) for s in stages.values() if isinstance(s,dict))
    if total:progress={'completed':done,'total':total}
    exports=[{'id':str(x.pk),'state':x.state,'error':x.error,'url':reverse('word-review-file',kwargs={'pk':batch.pk,'export_id':x.pk}) if x.state=='ready' else None}
        for x in WordReviewExport.objects.filter(batch=batch).order_by('-created')[:3]]
    return {'batch':str(batch.pk),'state':batch.status,'text':text,'version':version,'complete':complete,'terminal':terminal,
            'actions':actions,'progress':progress,'clock':review_clock(batch,run),'exports':exports,
            'documents':[{'id':d.pk,'name':d.name,'role':d.review_role} for d in batch.documents.order_by('id')],
            'checks':batch.checks,'expert_url':reverse('dashboard')+'?presentation=expert&batch='+str(batch.pk)+'#current-review'}

def serialize(user,c):
    state=compact(user,c)
    if state:
        # One durable, mutable system message for this exact batch/run version.
        values={'role':'assistant','text':state['text'],'metadata':{'kind':'progress','batch':state['batch'],'version':state['version']}}
        existing=c.messages.filter(key=PROGRESS_KEY).first()
        if not existing or existing.text!=values['text'] or existing.metadata!=values['metadata']:
            ChatMessage.objects.update_or_create(conversation=c,key=PROGRESS_KEY,defaults=values)
    return {'id':str(c.pk),'title':c.title,'draft':c.draft,'batch':str(c.batch_id) if c.batch_id else None,
            'messages':[{'id':m.pk,'role':m.role,'text':m.text,'metadata':m.metadata} for m in c.messages.all()], 'state':state}

@login_required
def page(request):
    profile,_=AccessProfile.objects.get_or_create(user=request.user)
    cid=request.GET.get('conversation');batch_id=request.GET.get('batch');selected=None
    if cid:selected=owned(request.user,cid)
    elif batch_id:
        batch=visible_batch(request.user,batch_id)
        selected,_=Conversation.objects.get_or_create(batch=batch,owner=request.user,defaults={'title':batch.name})
    initial={'conversation':serialize(request.user,selected) if selected else None,'choices':choices(request.user),
             'presentation':getattr(request,'chat_presentation',None) or request.GET.get('presentation') or profile.presentation,'theme':profile.theme,
             'expert_url':getattr(request,'chat_expert_url','')}
    return render(request,'chat.html',{'initial':initial,'profile':profile,'default_review_prompt':DEFAULT_REVIEW_PROMPT})

@login_required
@require_POST
@checked_input
def preference(request):
    data=json.loads(request.body)
    with transaction.atomic():
        profile,_=AccessProfile.objects.select_for_update().get_or_create(user=request.user)
        for key,allowed in [('presentation',('chat','expert')),('theme',('light','dark'))]:
            if key in data:
                if data[key] not in allowed:return JsonResponse({'error':'Неизвестное предпочтение'},status=400)
                setattr(profile,key,data[key])
        profile.save(update_fields=['presentation','theme'])
    return JsonResponse({'presentation':profile.presentation,'theme':profile.theme})

@login_required
@checked_input
def conversations(request):
    if request.method=='POST':
        data=json.loads(request.body or '{}')
        if data.get('batch'):
            batch=visible_batch(request.user,data['batch'])
            c,_=Conversation.objects.get_or_create(batch=batch,owner=request.user,defaults={'title':batch.name})
        else:
            try:cid=uuid.UUID(data.get('key') or str(uuid.uuid4()))
            except ValueError:return JsonResponse({'error':'Некорректный идентификатор'},status=400)
            previous=Conversation.objects.filter(pk=cid).first()
            if previous and previous.owner_id!=request.user.pk:raise PermissionDenied
            c,_=Conversation.objects.get_or_create(pk=cid,owner=request.user)
        return JsonResponse(serialize(request.user,c),status=201)
    existing=list(Conversation.objects.filter(owner=request.user).select_related('batch').order_by('-updated')[:100])
    represented={c.batch_id for c in existing}
    imports=[{'batch':str(b.pk),'title':b.name,'state':b.get_status_display()} for b in Batch.objects.filter(owner=request.user,archived=False).exclude(pk__in=represented).order_by('-created')[:100-len(existing)]]
    return JsonResponse({'items':[{'id':str(c.pk),'title':c.title,'state':c.batch.get_status_display() if c.batch else 'Черновик'} for c in existing]+imports})

@login_required
@checked_input
def conversation(request,cid):
    c=owned(request.user,cid)
    if request.method=='POST':
        data=json.loads(request.body)
        # File bytes and browser paths are deliberately never persisted as draft.
        c.draft={k:data[k] for k in ('text','checks','normative_sets','roles','experience') if k in data}
        if len(json.dumps(c.draft,ensure_ascii=False))>12000:return JsonResponse({'error':'Черновик слишком велик'},status=400)
        c.save(update_fields=['draft','updated'])
    return JsonResponse(serialize(request.user,c))

@login_required
@require_POST
@checked_input
def send(request,cid):
    c=owned(request.user,cid);text=review_prompt(request.POST.get('text',''))
    if len(text)>MAX_TEXT:return JsonResponse({'error':'Введите запрос до 6000 символов.'},status=422)
    try:key=uuid.UUID(request.POST.get('key',''))
    except ValueError:return JsonResponse({'error':'Нужен идентификатор сообщения'},status=400)
    with transaction.atomic():
        c=Conversation.objects.select_for_update().get(pk=c.pk)
        old=c.messages.filter(key=key).first()
        if old:
            if old.text!=text:return JsonResponse({'error':'Это сообщение уже принято с другим текстом.'},status=409)
            if not old.metadata.get('pending'):return JsonResponse(serialize(request.user,c))
        action=intent(text)
        if isinstance(action,str) or c.batch_id:
            c.messages.create(key=key,role='user',text=text)
            reply='Подробный разбор доступен через нижний переключатель «Эксперт».' if action=='details' else 'Состояние проверки показано ниже.' if c.batch_id else 'Прикрепите документы и напишите «Проверь». Для запуска нужны явно выбранные роли документов.'
            if c.batch_id and isinstance(action,list):reply='Для нового запуска используйте «Повторить» или начните новую переписку. Существующий результат сохранён.'
            c.messages.create(role='assistant',text=reply)
            return JsonResponse(serialize(request.user,c))
        files=request.FILES.getlist('documents')
        def clarify(question):
            c.messages.get_or_create(key=key,defaults={'role':'user','text':text,'metadata':{'pending':True}})
            c.messages.update_or_create(key=uuid.uuid5(key,'clarification'),defaults={'role':'assistant','text':question,'metadata':{'kind':'clarification'}})
            return JsonResponse({'clarification':question,'conversation':serialize(request.user,c)},status=422)
        try:config=json.loads(request.POST.get('config','{}'))
        except ValueError:return JsonResponse({'error':'Некорректные параметры'},status=400)
        roles=config.get('roles',[])
        if not files:return clarify('Прикрепите документ для проверки.')
        if len(roles)!=len(files) or any(r not in ('target','approved_reference') for r in roles) or 'target' not in roles:
            return clarify('Какие документы проверить, а какие использовать как основание? Выберите роль у каждого вложения.')
        selected=choices(request.user);checks=config.get('checks') or action
        sets=config.get('normative_sets',[])
        if 'sto' in checks and not sets:
            if len(selected['norms'])==1:sets=[selected['norms'][0]['id']]
            else:return clarify('Выберите нормативную базу.' if selected['norms'] else 'Проверка по СТО недоступна: опубликованной базы нет. Выберите доступные направления.')
        data={'name':text[:160],'user_prompt':text,'checks':checks,'normative_sets':sets,'experience':config.get('experience',''),'launch_key':str(key)}
        form=launch_form(request.user,data)
        if not form.is_valid():return JsonResponse({'error':'; '.join(str(e) for errors in form.errors.values() for e in errors)},status=422)
        cleaned=dict(form.cleaned_data,document_roles=roles)
        failures=(ValueError,OSError)
        if settings.KNOWLEDGE_V2_ENABLED:
            from knowledge.services import Conflict,NotReady
            failures+=(Conflict,NotReady)
        try:batch,_=launch_uploaded(request.user,cleaned,files)
        except failures as error:return JsonResponse({'error':str(error)},status=422)
        c.batch=batch;c.title=batch.name;c.draft={};c.save(update_fields=['batch','title','draft','updated'])
        c.messages.update_or_create(key=key,defaults={'role':'user','text':text,'metadata':{'documents':[{'id':d.pk,'name':d.name,'role':d.review_role} for d in batch.documents.order_by('id')],'checks':batch.checks,'normative_sets':sets}})
        names=dict(CHECKS);basis=', '.join(f.name for f,r in zip(files,roles) if r=='approved_reference') or 'не приложены'
        c.messages.create(role='assistant',text='Запуск принят. '+', '.join(names[k] for k in batch.checks)+'.\nОснования: '+basis+'.\nНормативная база: '+(', '.join(n['name'] for n in selected['norms'] if n['id'] in sets) or 'не используется')+'.',metadata={'kind':'launch','batch':str(batch.pk)})
    return JsonResponse(serialize(request.user,c))

@login_required
@require_POST
@checked_input
def control(request,cid):
    c=owned(request.user,cid);batch=c.batch
    if not batch or not can_edit(request.user,batch):raise PermissionDenied
    data=json.loads(request.body);action=data.get('action')
    if action not in ('pause','resume','cancel','rerun'):return JsonResponse({'error':'Неизвестное действие'},status=400)
    from .views import batch_action
    original=request.POST;request.POST=__import__('django.http',fromlist=['QueryDict']).QueryDict('action='+action)
    response=batch_action(request,batch.pk);request.POST=original
    if response.status_code>=400:return JsonResponse({'error':response.content.decode()},status=response.status_code)
    if action=='rerun':
        repeated=batch.reruns.filter(status__in=('prepared','waiting','preparing','running','paused')).first()
        if not repeated:return JsonResponse({'error':'Повтор не создан'},status=409)
        new,_=Conversation.objects.get_or_create(batch=repeated,owner=request.user,defaults={'title':repeated.name})
        if repeated.status=='prepared':
            form=launch_form(request.user)
            if settings.KNOWLEDGE_V2_ENABLED:
                from knowledge.launch import LaunchForm,start
                form=LaunchForm(user=request.user,batch=repeated)
                values={'checks':repeated.checks,'normative_sets':form.initial.get('normative_sets',[]),'experience':form.initial.get('experience',''),'launch_key':str(new.pk),'logging_enabled':repeated.logging_enabled}
                bound=LaunchForm(values,user=request.user,batch=repeated)
                if not bound.is_valid():return JsonResponse({'clarification':'Для повтора подтвердите доступную базу в режиме «Эксперт».','conversation':serialize(request.user,new)},status=422)
                start(request.user,repeated.pk,bound.cleaned_data['checks'],bound.cleaned_data['normative_sets'],bound.cleaned_data.get('experience'),bound.cleaned_data['launch_key'],logging_enabled=bound.cleaned_data['logging_enabled'])
        return JsonResponse(serialize(request.user,new))
    batch.refresh_from_db();return JsonResponse(serialize(request.user,c))

@login_required
def export(request,cid,kind):
    c=owned(request.user,cid);batch=c.batch
    if not batch:return HttpResponse(status=409)
    state=compact(request.user,c)
    if not state['terminal']:return HttpResponse('Итог ещё не сформирован',status=409)
    from .review_export_views import capture,make_plans,fingerprint,VERSION,ReviewError
    snapshot,version=capture(request.user,batch)
    if kind=='summary':
        response=HttpResponse(c.title+'\n'+state['text']+'\nВерсия: '+version,content_type='text/plain; charset=utf-8')
        response['Content-Disposition']='attachment; filename="normcontrol-summary.txt"';return response
    if kind=='xlsx':
        from .report_export import make_xlsx
        from .report_export import task_errors
        snapshot,version=capture(request.user,batch,include_resolved=True)
        run=WorkerRun.objects.filter(batch=batch).first()
        report=dict(run.report if run else {},documents=[{'id':d.sha256,'name':d.name} for d in batch.documents.all()])
        limitations=['Полный охват не подтверждён.'] if not state['complete'] else []
        for s in snapshot['states'].values():limitations.extend(s.get('summary',{}).get('limitations',[]))
        errors=task_errors(run) if run else []
        for s in snapshot['states'].values():
            errors.extend({'id':k,'stage':'sto','stage_label':'СТО','state':'failed','attempts':1,'error':v.get('error','Неизвестная причина') if isinstance(v,dict) else str(v)} for k,v in s.get('summary',{}).get('errors',{}).items())
        content=make_xlsx(batch,SimpleNamespace(report=report,snapshot={}),snapshot['findings'],errors,limitations,'Все замечания; версия '+version)
        response=HttpResponse(content,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']='attachment; filename="normcontrol-register.xlsx"';return response
    if kind!='word' or request.method!='POST':return HttpResponse(status=405)
    if not can_edit(request.user,batch):raise PermissionDenied
    try:
        ids=list(batch.documents.filter(review_role='target').values_list('pk',flat=True))
        if not ids:raise ReviewError('Назначьте роли документов в режиме «Эксперт».')
        plans=make_plans(batch,snapshot,version,ids)
        key=fingerprint({'version':version,'generator':VERSION,'plans':[{k:v for k,v in p.items() if k!='created'} for p in plans]})
        with transaction.atomic():
            Batch.objects.select_for_update().get(pk=batch.pk)
            record,_=WordReviewExport.objects.get_or_create(batch=batch,key=key,defaults={'owner':request.user,'snapshot':snapshot,'plans':plans})
            if record.state in ('planned','failed','waiting_ram'):
                record.state='queued';record.error='';record.save(update_fields=['state','error'])
        from .review_export_queue import kick
        kick()
        return JsonResponse({'id':str(record.pk),'state':record.state})
    except ReviewError as e:return JsonResponse({'error':str(e)},status=409)
