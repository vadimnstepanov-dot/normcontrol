"""One screen and one atomic, idempotent confirmation for upload and launch."""
import hashlib,json,uuid
from pathlib import Path
from django import forms
from django.conf import settings
from django.db import transaction
from django.db.models import Max,Sum
from .forms import CHECKS
from .models import Batch,Document,LaunchReceipt,Audit

def form_context(form):
    norms=[]
    if getattr(form,'norms',[]):
        from knowledge.curation import summary
        for item in form.norms:
            info=summary(item.active_release);quality=info.get('quality') or {}
            norms.append({'id':str(item.pk),'name':item.name,'requirements':quality.get('ready',info.get('total',0)),
                'candidates':quality.get('candidate',0)})
    return {'norms':norms,'normative_scopes':{str(x.pk):str(x.scope_id) for x in getattr(form,'norms',[])},
        'experience_scopes':{k:str(x.scope_id) for k,x in getattr(form,'experiences',{}).items()}}

def launch_response(request,batch,created=True):
    from django.http import JsonResponse
    from django.shortcuts import redirect
    from django.urls import reverse
    from django.contrib import messages
    target=reverse('dashboard')+'?batch='+str(batch.pk)+'#current-review'
    messages.success(request,'Проверка поставлена в очередь. Все выбранные направления включены.' if created else 'Этот запуск уже принят. Открыта существующая проверка.')
    return JsonResponse({'next':target}) if request.headers.get('X-Requested-With')=='XMLHttpRequest' else redirect(target)

class UploadForm(forms.Form):
    user_prompt=forms.CharField(label='Задание для проверки',max_length=6000,required=False,
        widget=forms.Textarea(attrs={'rows':3,'placeholder':'Например: проверь ЧТЗ по СТО, грамматике и логике; ТЗ используй как основание.'}))
    name=forms.CharField(label='Название проверки',max_length=160,required=False,
        widget=forms.TextInput(attrs={'placeholder':'Необязательно — используем название первого файла'}))
    checks=forms.MultipleChoiceField(label='Направления проверки',choices=CHECKS,
        widget=forms.CheckboxSelectMultiple,initial=['sto','logic','language'])
    launch_key=forms.UUIDField(widget=forms.HiddenInput)
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.initial['launch_key']=uuid.uuid4()

def launch_uploaded(user,data,files):
    from .views import inspect_word
    if not 1<=len(files)<=20:raise ValueError('Добавьте от 1 до 20 документов.')
    if sum(f.size for f in files)>100*1024*1024:raise ValueError('Общий размер пакета — не более 100 МБ.')
    prepared=[]
    roles=data.get('document_roles') or ['target']*len(files)
    if len(roles)!=len(files) or any(r not in ('target','approved_reference') for r in roles) or 'target' not in roles:
        raise ValueError('Укажите роль каждого документа; нужен хотя бы один проверяемый документ.')
    for file in files:
        try:prepared.append((file,inspect_word(file)))
        except ValueError as error:raise ValueError(f'«{Path(file.name.replace(chr(92),"/")).name}»: {error}') from error
    content_roles={}
    for (_,meta),role in zip(prepared,roles):
        previous_role=content_roles.setdefault(meta['sha256'],role)
        if previous_role!=role:
            raise ValueError('Одинаковый документ назначен одновременно проверяемым и основанием. Оставьте одну роль для этой копии.')
    directions=[key for key,_ in CHECKS if key in data['checks']]
    title=data.get('name') or Path(files[0].name.replace('\\','/')).stem[:160]
    fingerprint=hashlib.sha256(json.dumps({'name':title,'checks':directions,
        'user_prompt':data.get('user_prompt',''),
        'sets':sorted(data.get('normative_sets',[])),'experience':data.get('experience') or '',
        'v2':settings.KNOWLEDGE_V2_ENABLED,'logging_enabled':bool(data.get('logging_enabled')),
        'roles':[(m['sha256'],r) for (_,m),r in zip(prepared,roles)],
        'documents':sorted((Path(f.name.replace('\\','/')).name[:240],f.size,m['sha256']) for f,m in prepared)},
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    saved=[]
    try:
        with transaction.atomic():
            previous=LaunchReceipt.objects.select_related('batch').filter(user=user,key=data['launch_key']).first()
            if previous:
                if previous.fingerprint!=fingerprint:raise ValueError('Этот запуск уже подтверждён с другим составом или настройками. Откройте новую проверку.')
                return previous.batch,False
            used=Document.objects.values('file').annotate(stored_size=Max('size')).aggregate(total=Sum('stored_size'))['total'] or 0
            if used+sum(f.size for f in files)>2*1024**3:raise ValueError('Хранилище заполнено. Обратитесь к администратору.')
            batch=Batch.objects.create(owner=user,name=title,checks=directions,status='prepared')
            for (f,meta),role in zip(prepared,roles):
                doc=Document(batch=batch,name=Path(f.name.replace('\\','/')).name[:240],size=f.size,review_role=role,**meta)
                doc.file.save(f.name,f,save=False);saved.append((doc.file.storage,doc.file.name));doc.save()
            batch.review_scope={str(d.pk):d.review_role for d in batch.documents.all()}
            batch.save(update_fields=['review_scope'])
            if settings.KNOWLEDGE_V2_ENABLED:
                from knowledge.launch import start
                start(user,batch.pk,directions,data.get('normative_sets',[]),data.get('experience'),data['launch_key'],logging_enabled=bool(data.get('logging_enabled')))
                batch.refresh_from_db()
            else:
                batch.status='waiting'
                batch.queue_position=(Batch.objects.filter(status='waiting').aggregate(n=Max('queue_position'))['n'] or 0)+1
                batch.save(update_fields=['status','queue_position'])
            LaunchReceipt.objects.create(user=user,key=data['launch_key'],fingerprint=fingerprint,batch=batch)
            Audit.objects.create(user=user,action='Запущена проверка «'+title[:160]+'»')
            return batch,True
    except BaseException:
        for storage,name in saved:storage.delete(name)
        raise
