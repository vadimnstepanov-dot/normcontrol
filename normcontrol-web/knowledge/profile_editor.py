import json
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import render, redirect, get_object_or_404
from .models import Scope, DocumentProfile, Command
from .profiles import save_profile
from .services import Conflict


class ProfileForm(forms.Form):
    name=forms.CharField(label='Название профиля',max_length=160)
    description=forms.CharField(label='Когда и на каком основании применять',widget=forms.Textarea(attrs={'rows':3}),max_length=4000)
    parents=forms.MultipleChoiceField(label='Наследовать требования профилей',required=False,widget=forms.CheckboxSelectMultiple)
    bindings=forms.MultipleChoiceField(label='Требования из нормативных источников',required=False,widget=forms.CheckboxSelectMultiple)
    organization=forms.CharField(label='Организация',required=False,help_text='Например: РЖД. Значение должно подтверждаться документом или настройками проекта.')
    document_type=forms.CharField(label='Вид документа',required=False,help_text='Для общего профиля оставьте пустым; для приложения укажите вид документа.')
    stage=forms.CharField(label='Стадия работ',required=False)
    work_type=forms.CharField(label='Вид работ',required=False)
    revision=forms.IntegerField(widget=forms.HiddenInput,required=False)
    advanced=forms.JSONField(label='Дополнительные условия применимости',required=False,widget=forms.Textarea(attrs={'rows':4}),
        help_text='Необязательно. Декларативные all_of / any_of / not / fact; выполнение кода запрещено.')


def editor(request):
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:raise PermissionDenied('Только администратор')
    if request.method not in ('GET','POST'):return HttpResponseNotAllowed(['GET','POST'])
    scope=get_object_or_404(Scope,pk=request.GET.get('scope')) if request.GET.get('scope') else Scope.objects.order_by('name').first()
    current=get_object_or_404(DocumentProfile,pk=request.GET['profile'],scope=scope) if request.GET.get('profile') else None
    profiles=DocumentProfile.objects.filter(scope=scope).order_by('name') if scope else DocumentProfile.objects.none()
    binding_options={}
    if scope:
        for c in Command.objects.filter(normative_set__scope=scope,kind='source.analyze',state='done').order_by('created'):
            for p in c.result.get('summary',{}).get('profiles',[]):
                key=c.payload['source_id']+'|'+p['id'];binding_options[key]=(c.payload['source_id'],p['id'],p['name'])
    initial={}
    if current:
        initial=dict(name=current.name,description=current.definition['description'],parents=current.definition['parents'],
            bindings=[b['source_id']+'|'+b['profile_id'] for b in current.definition['bindings']],
            revision=current.revision)
        expression=current.definition['expression'];rest=[]
        for term in expression.get('all_of',[expression]):
            fact=term.get('fact',{})
            name=fact.get('name');values=fact.get('in',[])
            if name in ('organization','document_type','stage','work_type') and len(values)==1 and isinstance(values[0],str) and name not in initial:
                initial[name]=values[0]
            else:rest.append(term)
        if rest:initial['advanced']=rest[0] if len(rest)==1 else {'all_of':rest}
    form=ProfileForm(request.POST if request.method=='POST' else None,initial=initial)
    form.fields['parents'].choices=[(str(p.pk),p.name) for p in profiles if not current or p.pk!=current.pk]
    form.fields['bindings'].choices=[(k,v[2]+' · '+v[0][:8]) for k,v in binding_options.items()]
    if request.method=='POST' and scope and form.is_valid():
        d=form.cleaned_data;predicates=[]
        for name in ('organization','document_type','stage','work_type'):
            if d[name]:predicates.append({'fact':{'name':name,'in':[d[name]]}})
        if d['advanced']:predicates.append(d['advanced'])
        expression=(predicates[0] if len(predicates)==1 else {'all_of':predicates}) if predicates else {'unknown':'Условия применимости ещё не заданы администратором'}
        definition=dict(name=d['name'],description=d['description'],parents=d['parents'],expression=expression,
            bindings=[dict(source_id=binding_options[k][0],profile_id=binding_options[k][1]) for k in d['bindings']])
        try:
            row=save_profile(request.user,scope.pk,definition,current.pk if current else None,d['revision'])
            messages.success(request,'Версия профиля сохранена. Предыдущие версии и результаты проверок сохранены.')
            return redirect(request.path+'?scope='+str(scope.pk)+'&profile='+str(row.pk))
        except Conflict:form.add_error(None,'Профиль уже изменён. Обновите страницу и повторите правку.')
        except (ValueError,ValidationError,ObjectDoesNotExist):form.add_error(None,'Проверьте условия, связи и источники: обнаружена неверная ссылка или циклическое наследование.')
    return render(request,'knowledge_profiles.html',dict(page='knowledge_profiles',scopes=Scope.objects.order_by('name'),scope=scope,
        profiles=profiles,current=current,form=form,history=current.revisions.order_by('-revision')[:10] if current else []))
