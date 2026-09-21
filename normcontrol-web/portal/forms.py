from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import Batch,LLMConfig

CHECKS=[('sto','Соответствие СТО'),('logic','Общая и техническая логика'),('language','Грамотность и терминология'),('formatting','Оформление по инструкции по делопроизводству')]
class BatchForm(forms.ModelForm):
    checks=forms.MultipleChoiceField(label='Направления проверки',choices=CHECKS,widget=forms.CheckboxSelectMultiple,initial=['sto','logic','language'])
    class Meta:model=Batch;fields=['name','checks'];labels={'name':'Название пакета'}

class LLMForm(forms.ModelForm):
    api_key=forms.CharField(label='API-ключ',required=False,widget=forms.PasswordInput,help_text='Оставьте пустым, чтобы сохранить текущий ключ.')
    clear_key=forms.BooleanField(label='Удалить сохранённый ключ',required=False)
    class Meta:
        model=LLMConfig
        fields=['name','endpoint','model','context_tokens','output_tokens','concurrency','timeout_seconds']
        labels={'name':'Название подключения','endpoint':'Адрес API (OpenAI-совместимый)','model':'Идентификатор модели','context_tokens':'Контекст, токенов','output_tokens':'Максимальный ответ, токенов','concurrency':'Одновременных запросов','timeout_seconds':'Ожидание ответа, секунд'}
    def clean(self):
        data=super().clean()
        for field,lo,hi in [('context_tokens',4096,1048576),('output_tokens',256,65536),('concurrency',1,8),('timeout_seconds',30,1800)]:
            value=data.get(field)
            if value is not None and not lo<=value<=hi:self.add_error(field,f'Допустимо от {lo} до {hi}.')
        if data.get('output_tokens',0)+2048>=data.get('context_tokens',20480):self.add_error('output_tokens','Оставьте место для документа и служебного резерва.')
        from urllib.parse import urlsplit
        url=urlsplit(data.get('endpoint',''))
        if url.username or url.password or url.query or url.fragment:self.add_error('endpoint','Используйте адрес без пароля, параметров и фрагмента. Ключ укажите отдельно.')
        return data

class CreateUserForm(UserCreationForm):
    first_name=forms.CharField(label='Имя',max_length=150)
    is_staff=forms.BooleanField(label='Администратор',required=False)
    class Meta:model=User;fields=['username','first_name','email','is_staff']

class AutoRegistrationForm(forms.Form):
    password1=forms.CharField(label='Пароль',strip=False,widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}))
    password2=forms.CharField(label='Повторите пароль',strip=False,widget=forms.PasswordInput(attrs={'autocomplete':'new-password'}))
    def __init__(self,*args,username='',**kwargs):
        super().__init__(*args,**kwargs);self.username=username
    def clean(self):
        data=super().clean();first=data.get('password1');second=data.get('password2')
        if first and second and first!=second:self.add_error('password2','Пароли не совпадают.')
        if first:
            try:validate_password(first,User(username=self.username))
            except ValidationError as e:self.add_error('password1',e)
        return data
