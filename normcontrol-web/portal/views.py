import hashlib,os,zipfile,io,xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate,login,logout,update_session_auth_hash
from django.contrib.auth.decorators import login_required,user_passes_test
from django.contrib.auth.forms import PasswordChangeForm,SetPasswordForm
from django.contrib.auth.models import User
from django.db import transaction,IntegrityError
from django.db.models import Sum,Max,F
from django.http import FileResponse,HttpResponseForbidden,JsonResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Batch,Document,LLMConfig,Audit,LoginAttempt,AccessProfile,WorkerPresence
from .forms import BatchForm,LLMForm,CreateUserForm,AutoRegistrationForm
from .access import visible_batches,editable_batches,can_edit

SELF_REGISTRATION_PREFIX='RZDTECH/'
SELF_REGISTRATION_SESSION='self_registration_username'

def valid_self_registration_name(name):
    suffix=name[len(SELF_REGISTRATION_PREFIX):] if name.startswith(SELF_REGISTRATION_PREFIX) else ''
    return bool(suffix and len(name)<=150 and name==name.strip() and not any(c.isspace() or ord(c)<32 for c in suffix))

def common(request):
    worker=WorkerPresence.objects.filter(heartbeat__gte=timezone.now()-timedelta(seconds=120)).order_by('-heartbeat').first()
    return {'site_name':settings.SITE_NAME,'knowledge_v2_enabled':settings.KNOWLEDGE_V2_ENABLED,'worker_online':bool(worker),'worker_state':worker.state if worker else 'offline',
        'rag_status':(worker.details or {}).get('rag') if worker else None}
def audit(request,text):Audit.objects.create(user=request.user,action=text)
def batches(request):return visible_batches(request.user)
def administrator(view):return login_required(user_passes_test(lambda u:u.is_staff,login_url='dashboard')(view))
def next_queue_position():return (Batch.objects.filter(status='waiting').aggregate(value=Max('queue_position'))['value'] or 0)+1

def sign_in(request):
    if request.user.is_authenticated:return redirect('dashboard')
    error=''
    if request.method=='POST':
        name=request.POST.get('username','')[:150];password_value=request.POST.get('password','');ip=request.META.get('REMOTE_ADDR','')
        if ip in ('127.0.0.1','::1'):ip=request.META.get('HTTP_X_FORWARDED_FOR',ip).split(',')[-1].strip()
        identity=hashlib.sha256(name.lower().encode()).hexdigest();cutoff=timezone.now()-timedelta(minutes=15)
        attempts=LoginAttempt.objects.filter(created__gte=cutoff)
        if attempts.filter(identity=identity).count()>=8 or attempts.filter(ip=ip).count()>=40:error='Слишком много попыток. Повторите вход через 15 минут.'
        elif password_value=='' and name.startswith(SELF_REGISTRATION_PREFIX):
            if not valid_self_registration_name(name):error='После RZDTECH/ укажите имя пользователя без пробелов.'
            elif User.objects.filter(username__iexact=name).exists():error='Учётная запись уже зарегистрирована. Введите пароль.'
            else:
                request.session[SELF_REGISTRATION_SESSION]=name
                return redirect('register')
        else:
            user=authenticate(request,username=name,password=password_value)
            if user:
                login(request,user);LoginAttempt.objects.filter(identity=identity).delete();audit(request,'Вход в систему');return redirect('dashboard')
            LoginAttempt.objects.create(identity=identity,ip=ip);error='Проверьте логин и пароль.'
        LoginAttempt.objects.filter(created__lt=timezone.now()-timedelta(days=1)).delete()
    return render(request,'login.html',{'error':error})

def self_register(request):
    if request.user.is_authenticated:return redirect('dashboard')
    name=request.session.get(SELF_REGISTRATION_SESSION,'')
    if not valid_self_registration_name(name):return redirect('login')
    if User.objects.filter(username__iexact=name).exists():
        request.session.pop(SELF_REGISTRATION_SESSION,None)
        return render(request,'login.html',{'error':'Учётная запись уже зарегистрирована. Введите пароль.'})
    form=AutoRegistrationForm(request.POST or None,username=name)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                if User.objects.filter(username__iexact=name).exists():raise IntegrityError('duplicate username')
                user=User.objects.create_user(username=name,password=form.cleaned_data['password1'])
                AccessProfile.objects.create(user=user,must_change_password=False)
        except IntegrityError:
            request.session.pop(SELF_REGISTRATION_SESSION,None)
            return render(request,'login.html',{'error':'Учётная запись уже зарегистрирована. Введите пароль.'})
        request.session.pop(SELF_REGISTRATION_SESSION,None)
        login(request,user);audit(request,'Саморегистрация пользователя '+name)
        messages.success(request,'Учётная запись создана.');return redirect('dashboard')
    return render(request,'register.html',{'form':form,'registration_username':name})

@require_POST
def sign_out(request):logout(request);return redirect('login')

@login_required
def dashboard(request):
    qs=batches(request);archived=request.GET.get('archive')=='1';active=qs.filter(archived=archived).order_by('-created','-pk')
    query=request.GET.get('q','')[:160]
    if query:active=active.filter(name__icontains=query)
    state=request.GET.get('state','')
    if state in dict(Batch._meta.get_field('status').choices):active=active.filter(status=state)
    visible=list(active.prefetch_related('documents','owner')[:100])
    selected=None
    selected_id=request.GET.get('batch','')
    if selected_id:
        selected=qs.filter(pk=selected_id).prefetch_related('documents').select_related('worker_run').first()
    if selected is None:
        selected=qs.filter(archived=False,status__in=('waiting','preparing','running','paused')).prefetch_related('documents').select_related('worker_run').first()
    if selected is None:
        selected=qs.filter(archived=False,worker_run__isnull=False).prefetch_related('documents').select_related('worker_run').first()
    if selected is None and visible:selected=visible[0]
    selected_knowledge=None;pending_knowledge=None
    if settings.KNOWLEDGE_V2_ENABLED:
        from django.core.exceptions import PermissionDenied
        from knowledge.models import KnowledgeCheck
        from knowledge.checks import visible as check_visible
        labels={'queued':'В очереди','running':'Проверяется','paused':'Приостановлена',
                'completed':'Завершена','partial':'Завершена с ограничениями','failed':'Ошибка выполнения'}
        by_batch={}
        ids={x.pk for x in visible}
        if selected:ids.add(selected.pk)
        jobs=KnowledgeCheck.objects.filter(batch_id__in=ids).select_related(
            'snapshot','experience_release__normative_set__scope').order_by('-created')
        for job in jobs:
            if job.batch_id in by_batch:continue
            try:check_visible(request.user,job)
            except PermissionDenied:continue
            job.display_label=labels.get(job.state,job.state)
            by_batch[job.batch_id]=job
        for item in visible:
            item.knowledge_current=by_batch.get(item.pk)
            if item.knowledge_current and item.knowledge_current.state=='queued' and item.status in ('waiting','preparing','running','paused'):
                item.knowledge_current.display_label=item.get_status_display()+' · СТО в очереди'
        # Prefer a visible live v2 check over the legacy batch projection.
        if not selected_id:
            current=next((j for j in by_batch.values() if j.state in ('queued','running','paused')),None)
            if current:selected=qs.filter(pk=current.batch_id).prefetch_related('documents').first()
        if selected:selected_knowledge=by_batch.get(selected.pk)
        if selected_knowledge and selected_knowledge.state=='queued':
            from .models import WorkerRun
            run=WorkerRun.objects.filter(batch=selected,state__in=('claimed','preparing','running','paused')).first()
            if run or selected.status=='waiting':
                pending_knowledge=selected_knowledge;selected_knowledge=None
    expand_batches=bool(query or state or (selected_id and selected and any(item.pk==selected.pk for item in visible[3:])))
    preview=[]
    if selected and hasattr(selected,'worker_run'):
        preview=selected.worker_run.report.get('findings',[])[:60]
    return render(request,'dashboard.html',{'page':'batches','batches':visible,'selected_batch':selected,'preview_findings':preview,
        'selected_knowledge':selected_knowledge,'pending_knowledge':pending_knowledge,'job':selected_knowledge,'can_manage':bool(selected and can_edit(request.user,selected)),
        'expand_batches':expand_batches,'hidden_batch_count':max(len(visible)-3,0),
        'can_manage_selected':bool(selected and can_edit(request.user,selected)),
        'total':qs.filter(archived=False).count(),'waiting':qs.filter(archived=False,status='waiting').count(),
        'document_count':Document.objects.filter(batch__in=qs.filter(archived=False)).count(),'query':query,'state':state,'archived':archived})

def inspect_word(file):
    suffix=Path(file.name.replace('\\','/')).suffix.casefold()
    if suffix not in ('.doc','.docx'):raise ValueError('Поддерживаются документы Word .doc и .docx.')
    if file.size>50*1024*1024:raise ValueError('Один документ не должен превышать 50 МБ.')
    checksum=hashlib.sha256()
    for chunk in file.chunks():checksum.update(chunk)
    file.seek(0)
    if suffix=='.doc':
        signature=file.read(8);file.seek(0)
        if signature!=b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':raise ValueError('Файл не является корректным двоичным документом Word .doc.')
        return {'sha256':checksum.hexdigest(),'paragraphs':0,'tables':0,'outline':[]}
    try:
        with zipfile.ZipFile(file) as z:
            if len(z.infolist())>5000 or sum(x.file_size for x in z.infolist())>100*1024*1024:raise ValueError('Документ слишком велик после распаковки.')
            if any(n.lower().endswith('vbaproject.bin') for n in z.namelist()):raise ValueError('Документы с макросами не принимаются.')
            with z.open('word/document.xml') as source:prefix=source.read(4096)
            if b'<!DOCTYPE' in prefix.upper() or b'<!ENTITY' in prefix.upper():raise ValueError('Неподдерживаемая структура документа.')
            # Full XML validation, numbering, tables and layout are processed on the desktop.
            return {'sha256':checksum.hexdigest(),'paragraphs':0,'tables':0,'outline':[]}
    except (zipfile.BadZipFile,KeyError,ET.ParseError,RuntimeError):raise ValueError('Файл не является корректным документом Word .docx.')
    finally:file.seek(0)

@login_required
def new_batch(request):
    from .intake import UploadForm,launch_uploaded,form_context,launch_response
    from django.core.exceptions import PermissionDenied
    data=request.POST if request.method=='POST' else None
    form=UploadForm(data)
    failures=(ValueError,OSError)
    if settings.KNOWLEDGE_V2_ENABLED:
        from knowledge.launch import NewLaunchForm
        from knowledge.services import Conflict,NotReady
        form=NewLaunchForm(data,user=request.user);failures+=(Conflict,NotReady)
    if request.method=='POST':
        if form.is_valid():
            try:
                batch,created=launch_uploaded(request.user,form.cleaned_data,request.FILES.getlist('documents'))
                return launch_response(request,batch,created)
            except PermissionDenied:form.add_error(None,'Доступ к нормативной базе изменился. Обновите страницу и проверьте выбранные наборы.')
            except failures as e:
                form.add_error(None,'Не удалось сохранить документы. Повторите попытку.' if isinstance(e,OSError) else str(e))
        if request.headers.get('X-Requested-With')=='XMLHttpRequest':
            return JsonResponse({'errors':form.errors.get_json_data()},status=422)
    return render(request,'launch.html',{'form':form,'page':'batches','can_manage':True,**form_context(form)})

@login_required
def detail(request,pk):
    batch=get_object_or_404(batches(request).prefetch_related('documents'),pk=pk)
    normative=None
    if settings.KNOWLEDGE_V2_ENABLED:
        from knowledge.models import KnowledgeCheck
        from knowledge.checks import visible as check_visible
        from django.core.exceptions import PermissionDenied
        for job in KnowledgeCheck.objects.filter(batch=batch).order_by('-created')[:10]:
            try:
                check_visible(request.user,job)
                job.display_label={'queued':'В очереди','running':'Проверяется','paused':'Приостановлена','completed':'Завершена','partial':'С ограничениями','failed':'Ошибка'}.get(job.state,job.state)
                normative=job;break
            except PermissionDenied:continue
    return render(request,'batch.html',{'batch':batch,'page':'batches','can_manage':can_edit(request.user,batch),'normative_job':normative})

@login_required
@require_POST
def add_documents(request,pk):
    source=get_object_or_404(editable_batches(request.user).prefetch_related('documents'),pk=pk)
    if not source.can_rerun or source.archived:return HttpResponseForbidden('Добавление файлов доступно после остановки или завершения проверки')
    files=request.FILES.getlist('documents');existing=list(source.documents.all());saved=[]
    try:
        if request.POST.get('confirm_restart')!='1':raise ValueError('Подтвердите полный перезапуск нормоконтроля после добавления файлов.')
        if source.reruns.filter(status__in=('prepared','waiting','preparing','running','paused')).exists():raise ValueError('Для этого пакета уже подготовлена или выполняется новая проверка. Остановите её либо добавьте документы в последнюю версию.')
        if not files:raise ValueError('Выберите хотя бы один документ Word.')
        if len(existing)+len(files)>20:raise ValueError('В одном пакете допускается не более 20 документов.')
        if sum(d.size for d in existing)+sum(f.size for f in files)>100*1024*1024:raise ValueError('Общий размер пакета — не более 100 МБ.')
        used=Document.objects.values('file').annotate(stored_size=Max('size')).aggregate(total=Sum('stored_size'))['total'] or 0
        if used+sum(f.size for f in files)>2*1024**3:raise ValueError('Хранилище заполнено. Обратитесь к администратору.')
        prepared=[(f,inspect_word(f)) for f in files]
        with transaction.atomic():
            editable_batches(request.user).filter(pk=pk).update(status=F('status'))
            source=get_object_or_404(editable_batches(request.user).prefetch_related('documents'),pk=pk)
            if not source.can_rerun or source.archived:raise ValueError('Состояние пакета изменилось. Обновите страницу.')
            if source.reruns.filter(status__in=('prepared','waiting','preparing','running','paused')).exists():raise ValueError('Для этого пакета уже подготовлена или выполняется новая проверка.')
            revision=Batch.objects.create(owner=source.owner,name=(source.name+' — дополнен')[:160],profile=source.profile,checks=list(source.checks),status='prepared' if settings.KNOWLEDGE_V2_ENABLED else 'waiting',source_batch=source,fresh_review=True,queue_position=next_queue_position())
            Document.objects.bulk_create([Document(batch=revision,name=d.name,file=d.file.name,size=d.size,sha256=d.sha256,paragraphs=d.paragraphs,tables=d.tables,outline=d.outline) for d in source.documents.all()])
            for f,meta in prepared:
                doc=Document(batch=revision,name=Path(f.name.replace('\\','/')).name[:240],size=f.size,**meta)
                doc.file.save(f.name,f,save=False);saved.append(doc.file.path);doc.save()
            audit(request,'Дополнен пакет '+str(source.pk)+'; создана версия '+str(revision.pk))
        if settings.KNOWLEDGE_V2_ENABLED:
            messages.success(request,'Файлы добавлены. Подтвердите нормативы для новой полной проверки с первого этапа. Предыдущий отчёт сохранён.')
            return redirect('knowledge-check-start',batch_id=revision.pk)
        messages.success(request,'Файлы добавлены. Новая версия пакета поставлена в очередь на полный нормоконтроль с первого этапа.')
        return redirect('batch',pk=revision.pk)
    except ValueError as e:
        for path in saved:Path(path).unlink(missing_ok=True)
        messages.error(request,str(e));return redirect('batch',pk=source.pk)

@administrator
def delete_batch(request,pk):
    batch=get_object_or_404(Batch,pk=pk)
    return render(request,'delete_batch.html',{'batch':batch,'page':'batches'})

@login_required
@require_POST
def batch_action(request,pk):
    batch=get_object_or_404(editable_batches(request.user),pk=pk);action=request.POST.get('action')
    if settings.KNOWLEDGE_V2_ENABLED and action in ('pause','resume','cancel'):
        from knowledge.models import KnowledgeCheck
        from knowledge.checks import control
        job=KnowledgeCheck.objects.filter(batch=batch,state__in=('queued','running','paused')).order_by('-created').first()
        run=getattr(batch,'worker_run',None)
        if job and (not run or run.state in ('completed','partial','failed','cancelled')):
            if action=='cancel':
                if job.state!='paused':control(request.user,job.pk,'pause')
                batch.status='cancelled';batch.save(update_fields=['status'])
                messages.success(request,'Запрошена управляемая остановка нормативного этапа. Результаты сохраняются.')
            else:
                control(request.user,job.pk,action)
                messages.success(request,'Команда передана нормативному обработчику.')
            return redirect('batch',pk=pk)
    if action=='delete':
        if not request.user.is_staff:return HttpResponseForbidden('Удаление доступно только администратору')
        if request.POST.get('confirm_delete')!=str(batch.pk):return HttpResponseForbidden('Подтвердите удаление пакета')
        with transaction.atomic():
            Batch.objects.filter(pk=pk).update(status=F('status'));batch=Batch.objects.get(pk=pk)
            run=getattr(batch,'worker_run',None)
            if batch.status in ('waiting','preparing','running','paused') or (run and run.state not in ('completed','partial','failed','cancelled')):
                messages.error(request,'Сначала отмените проверку и дождитесь остановки обработчика.')
                return redirect('batch',pk=pk)
            files=[(d.file.storage,d.file.name) for d in batch.documents.all()]
            audit(request,'Удалён пакет '+str(batch.pk)+' «'+batch.name[:150]+'»')
            batch.delete()
        cleanup_failed=False
        for storage,name in files:
            if not Document.objects.filter(file=name).exists():
                try:storage.delete(name)
                except OSError:cleanup_failed=True
        messages.success(request,'Пакет и его отчёт удалены. Файлы, используемые другими проверками, сохранены.')
        if cleanup_failed:messages.warning(request,'Не удалось удалить часть файлов из хранилища; необходима проверка прав доступа.')
        return redirect('dashboard')
    if action in ('rerun','retry'):
        with transaction.atomic():
            # Acquire a write lock before checking for an existing repeat (also on SQLite).
            editable_batches(request.user).filter(pk=pk).update(status=F('status'))
            batch=get_object_or_404(editable_batches(request.user),pk=pk)
            if not batch.can_rerun:return HttpResponseForbidden('Дождитесь завершения или отмените текущую проверку')
            repeated=batch.reruns.filter(status__in=('prepared','waiting','preparing','running','paused')).first()
            if repeated:
                messages.info(request,'Повторная проверка уже поставлена в очередь или выполняется.')
                return redirect('knowledge-check-start',batch_id=repeated.pk) if settings.KNOWLEDGE_V2_ENABLED and repeated.status=='prepared' else redirect('batch',pk=repeated.pk)
            documents=list(batch.documents.all())
            if not documents or any(not d.file.storage.exists(d.file.name) for d in documents):
                messages.error(request,'Повторная проверка недоступна: исходные документы не найдены.')
                return redirect('batch',pk=batch.pk)
            repeated=Batch.objects.create(owner=batch.owner,name=batch.name[:140]+' — повтор',profile=batch.profile,checks=list(batch.checks),status='prepared' if settings.KNOWLEDGE_V2_ENABLED else 'waiting',source_batch=batch,fresh_review=True,queue_position=next_queue_position())
            # Uploaded files are immutable; keep references without copying large files on the VPS.
            Document.objects.bulk_create([Document(batch=repeated,name=d.name,file=d.file.name,size=d.size,sha256=d.sha256,paragraphs=d.paragraphs,tables=d.tables,outline=d.outline) for d in documents])
            audit(request,'Повторный нормоконтроль: '+str(batch.pk)+' → '+str(repeated.pk))
        if settings.KNOWLEDGE_V2_ENABLED:
            messages.success(request,'Повтор подготовлен. Подтвердите направления и актуальные нормативы. Предыдущий отчёт сохранён.')
            return redirect('knowledge-check-start',batch_id=repeated.pk)
        messages.success(request,'Повторный нормоконтроль поставлен в очередь. Будут использованы актуальные настройки и новые ответы LLM. Предыдущий отчёт сохранён.')
        return redirect('batch',pk=repeated.pk)
    if action=='queue' and batch.status in ('prepared','cancelled') and not batch.archived and not hasattr(batch,'worker_run'):
        if settings.KNOWLEDGE_V2_ENABLED:return redirect('knowledge-check-start',batch_id=batch.pk)
        batch.status='waiting';batch.queue_position=next_queue_position();messages.success(request,'Пакет поставлен в очередь. Анализ начнётся после подключения обработчика LLM.')
    elif action in ('pause','resume') and hasattr(batch,'worker_run'):
        if action=='pause' and batch.status not in ('running','preparing'):return HttpResponseForbidden('Проверка не выполняется')
        if action=='resume' and batch.status!='paused':return HttpResponseForbidden('Проверка не приостановлена')
        batch.worker_run.control=action;batch.worker_run.save(update_fields=['control']);messages.success(request,'Команда передана обработчику.')
    elif action=='cancel' and batch.status in ('waiting','running','preparing','paused'):batch.status='cancelled';messages.success(request,'Запрошена отмена. Текущая ограниченная задача завершится и обработчик остановится.')
    elif action=='archive':batch.archived=not batch.archived;messages.success(request,'Пакет восстановлен.' if not batch.archived else 'Пакет перемещён в архив.')
    else:return HttpResponseForbidden('Действие недоступно')
    batch.save();audit(request,'Пакет «'+batch.name+'»: '+action);return redirect('batch',pk=pk)

@login_required
def download(request,pk):
    doc=get_object_or_404(Document.objects.filter(batch__in=batches(request)),pk=pk)
    kind='application/msword' if doc.name.casefold().endswith('.doc') else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    return FileResponse(doc.file.open('rb'),as_attachment=True,filename=doc.name,content_type=kind)

@login_required
def reports(request):return render(request,'reports.html',{'page':'reports','batches':batches(request).filter(worker_run__isnull=False).select_related('worker_run','owner')[:100]})

@login_required
def rag(request):return render(request,'rag.html',{'page':'rag'})

@administrator
def llm(request):
    config=LLMConfig.objects.first() or LLMConfig()
    form=LLMForm(request.POST or None,instance=config)
    if request.method=='POST' and form.is_valid():
        item=form.save(commit=False)
        if form.cleaned_data['clear_key']:item.key_encrypted=''
        if form.cleaned_data['api_key']:
            key=os.getenv('APP_CRYPT_KEY')
            if not key:form.add_error('api_key','Хранилище ключей ещё не настроено.')
            else:item.key_encrypted=Fernet(key.encode()).encrypt(form.cleaned_data['api_key'].encode()).decode()
        if not form.errors:
            item.save();audit(request,'Изменены параметры подключения LLM');messages.success(request,'Параметры сохранены. Новые задания используют настройки обработчика; текущие задания сохраняют свой бюджет.');return redirect('llm')
    return render(request,'llm.html',{'form':form,'has_key':bool(config.key_encrypted),'connection':config.last_probe,'page':'llm'})

@administrator
@require_POST
def llm_probe(request):
    from .llm_connection import probe
    c=get_object_or_404(LLMConfig,pk=LLMConfig.objects.first().pk if LLMConfig.objects.exists() else 0)
    r=probe(c);audit(request,'Проверка подключения LLM: '+('доступна' if r['ok'] else 'недоступна'))
    messages.success(request,'Модель доступна. Контекст: '+str(r['actual_context'])+' токенов.') if r['ok'] else messages.error(request,r['error'])
    return redirect('llm')

@administrator
def users(request):
    form=CreateUserForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        user=form.save();AccessProfile.objects.create(user=user,must_change_password=True)
        audit(request,'Создан пользователь '+user.username);messages.success(request,'Пользователь создан. При первом входе потребуется сменить пароль.');return redirect('users')
    return render(request,'users.html',{'users':User.objects.select_related('accessprofile').order_by('username'),'form':form,'page':'users'})

@administrator
@require_POST
def toggle_view_others(request,pk):
    user=get_object_or_404(User,pk=pk)
    if user.is_staff:return HttpResponseForbidden('Администраторы уже видят все проверки.')
    with transaction.atomic():
        profile,_=AccessProfile.objects.select_for_update().get_or_create(user=user)
        profile.can_view_others=not profile.can_view_others
        profile.save(update_fields=['can_view_others'])
        audit(request,('Разрешён' if profile.can_view_others else 'Отозван')+' просмотр чужих проверок: '+user.username)
    messages.success(request,'Доступ к чужим проверкам включён.' if profile.can_view_others else 'Доступ к чужим проверкам отключён.')
    return redirect('users')

@administrator
@require_POST
def toggle_admin(request,pk):
    with transaction.atomic():
        user=get_object_or_404(User.objects.select_for_update(),pk=pk)
        if user==request.user and user.is_staff:return HttpResponseForbidden('Нельзя снять роль администратора у собственной учётной записи.')
        if user.is_staff and User.objects.filter(is_staff=True,is_active=True).count()<=1:return HttpResponseForbidden('Нельзя снять роль последнего активного администратора.')
        user.is_staff=not user.is_staff
        user.save(update_fields=['is_staff'])
        audit(request,('Назначен администратором: ' if user.is_staff else 'Снята роль администратора: ')+user.username)
    messages.success(request,'Роль администратора назначена.' if user.is_staff else 'Роль администратора снята.')
    return redirect('users')

@administrator
@require_POST
def toggle_user(request,pk):
    user=get_object_or_404(User,pk=pk)
    if user==request.user:return HttpResponseForbidden('Нельзя отключить собственную учётную запись.')
    user.is_active=not user.is_active;user.save();audit(request,('Включён ' if user.is_active else 'Отключён ')+user.username);return redirect('users')

@administrator
def reset_user(request,pk):
    user=get_object_or_404(User,pk=pk);form=SetPasswordForm(user,request.POST or None)
    if request.method=='POST' and form.is_valid():
        form.save();AccessProfile.objects.update_or_create(user=user,defaults={'must_change_password':True})
        audit(request,'Сброшен пароль пользователя '+user.username);messages.success(request,'Новый временный пароль установлен.');return redirect('users')
    return render(request,'password.html',{'form':form,'reset_user':user,'page':'users'})

@administrator
def queue(request):
    waiting=Batch.objects.filter(status='waiting',archived=False).select_related('owner').prefetch_related('documents').order_by('queue_position','created','pk')
    active=Batch.objects.filter(status__in=('preparing','running','paused'),archived=False).select_related('owner','worker_run').prefetch_related('documents').order_by('created')
    return render(request,'queue.html',{'waiting_batches':waiting,'active_batches':active,'page':'queue'})

@administrator
@require_POST
def queue_action(request,pk):
    action=request.POST.get('action','')
    with transaction.atomic():
        batch=get_object_or_404(Batch.objects.select_for_update(),pk=pk)
        if action in ('top','up','down'):
            if batch.status!='waiting' or batch.archived:return HttpResponseForbidden('Пакет не находится в очереди')
            rows=list(Batch.objects.select_for_update().filter(status='waiting',archived=False).order_by('queue_position','created','pk'))
            for position,item in enumerate(rows,1):
                if item.queue_position!=position:Batch.objects.filter(pk=item.pk).update(queue_position=position)
                item.queue_position=position
            index=next(i for i,item in enumerate(rows) if item.pk==batch.pk)
            if action=='top' and index>0:
                for item in rows[:index]:Batch.objects.filter(pk=item.pk).update(queue_position=F('queue_position')+1)
                Batch.objects.filter(pk=batch.pk).update(queue_position=1)
            elif action=='up' and index>0:
                neighbour=rows[index-1]
                Batch.objects.filter(pk=batch.pk).update(queue_position=neighbour.queue_position)
                Batch.objects.filter(pk=neighbour.pk).update(queue_position=batch.queue_position)
            elif action=='down' and index<len(rows)-1:
                neighbour=rows[index+1]
                Batch.objects.filter(pk=batch.pk).update(queue_position=neighbour.queue_position)
                Batch.objects.filter(pk=neighbour.pk).update(queue_position=batch.queue_position)
            messages.success(request,'Порядок очереди обновлён.')
        elif action=='cancel' and batch.status=='waiting':
            batch.status='cancelled';batch.save(update_fields=['status']);messages.success(request,'Пакет снят с очереди.')
        elif action in ('pause','resume','cancel') and batch.status in ('preparing','running','paused') and hasattr(batch,'worker_run'):
            if action=='pause' and batch.status not in ('preparing','running'):return HttpResponseForbidden('Проверка уже приостановлена')
            if action=='resume' and batch.status!='paused':return HttpResponseForbidden('Проверка не приостановлена')
            if action=='cancel':batch.status='cancelled';batch.save(update_fields=['status'])
            else:batch.worker_run.control=action;batch.worker_run.save(update_fields=['control'])
            messages.success(request,'Команда передана локальному обработчику.')
        else:return HttpResponseForbidden('Действие недоступно')
        audit(request,'Управление очередью: '+action+' · '+str(batch.pk))
    return redirect('queue')

@login_required
def password(request):
    form=PasswordChangeForm(request.user,request.POST or None)
    if request.method=='POST' and form.is_valid():
        user=form.save();update_session_auth_hash(request,user);AccessProfile.objects.update_or_create(user=user,defaults={'must_change_password':False})
        audit(request,'Изменён собственный пароль');messages.success(request,'Пароль изменён.');return redirect('dashboard')
    return render(request,'password.html',{'form':form,'page':'account'})

@administrator
def audit_log(request):return render(request,'audit.html',{'events':Audit.objects.select_related('user')[:150],'page':'audit'})

def health(request):
    worker=WorkerPresence.objects.order_by('-heartbeat').first()
    fresh=bool(worker and (timezone.now()-worker.heartbeat).total_seconds()<=30)
    return JsonResponse({'status':'ok','version':5,'llm_online':bool(fresh and worker.state in ('idle','busy'))})
