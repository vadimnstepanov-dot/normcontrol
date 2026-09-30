"""One explicit launch, two sequential executors, one immutable normative snapshot."""
import uuid
from django import forms
from django.db import transaction
from django.db.models import Max
from django.core.exceptions import PermissionDenied
from portal.models import Batch, WorkerRun
from portal.forms import CHECKS
from portal.access import can_edit
from .models import NormativeSet, KnowledgeCheck, Command
from .access import allowed
from . import checks
from .services import Conflict, NotReady,digest


class LaunchForm(forms.Form):
    checks=forms.MultipleChoiceField(label='Направления проверки',choices=CHECKS,
        widget=forms.CheckboxSelectMultiple)
    normative_sets=forms.MultipleChoiceField(label='Опубликованные нормативные наборы',
        required=False,widget=forms.CheckboxSelectMultiple)
    experience=forms.ChoiceField(label='Проверенный опыт рецензий',required=False)
    launch_key=forms.UUIDField(widget=forms.HiddenInput)
    logging_enabled=forms.BooleanField(label='Сохранять подробный лог проверки (.xlsx)',required=False)

    def __init__(self,*args,user,batch=None,**kwargs):
        super().__init__(*args,**kwargs)
        eligible=[x for x in NormativeSet.objects.select_related('scope','active_release').order_by('name')
            if __import__('knowledge.object_control',fromlist=['metadata']).metadata('area',x.pk)['enabled'] and allowed(user,x.scope,'read') and x.state=='ready' and x.active_release
            and x.active_release.state=='active']
        norms=[x for x in eligible if x.purpose=='normative'
            and x.active_release.manifest.get('versions',{}).get('curation_digest')]
        self.fields['normative_sets'].choices=[(str(x.pk),f'{x.name} · {x.scope.name}') for x in norms]
        self.fields['experience'].choices=[('','Без опыта')]+[(str(x.pk),x.name) for x in eligible if x.purpose=='experience']
        self.norms=norms
        self.experiences={str(x.pk):x for x in eligible if x.purpose=='experience'}
        self.initial.update(checks=list(batch.checks) if batch else ['sto','logic','language'],launch_key=uuid.uuid4())
        self.initial['logging_enabled']=batch.logging_enabled if batch else False
        if not batch:
            if len(norms)==1:self.initial['normative_sets']=[str(norms[0].pk)]
            return
        previous=Command.objects.filter(kind='review.execute',payload__batch_id=str(batch.pk)).order_by('-created').first()
        if not previous and batch.source_batch_id:
            previous=Command.objects.filter(kind='review.execute',payload__batch_id=str(batch.source_batch_id)).order_by('-created').first()
        if previous:
            self.initial.update(normative_sets=previous.payload.get('set_ids',[]),experience=previous.payload.get('experience_set_id') or '')

    def clean(self):
        data=super().clean()
        if 'sto' in data.get('checks',[]) and not data.get('normative_sets'):
            self.add_error('normative_sets','Для проверки СТО выберите хотя бы один опубликованный нормативный набор.')
        if 'sto' not in data.get('checks',[]):
            data['normative_sets']=[];data['experience']=''
            return data
        selected=[x for x in self.norms if str(x.pk) in data.get('normative_sets',[])]
        scopes={x.scope_id for x in selected}
        if len(selected)>20:self.add_error('normative_sets','Выберите не более 20 нормативных наборов.')
        experience=self.experiences.get(data.get('experience'))
        if experience and experience.scope_id not in scopes:
            self.add_error('experience','Опыт рецензий должен относиться к той же проектной области, что и нормативы.')
        return data


class NewLaunchForm(LaunchForm):
    name=forms.CharField(label='Название проверки',max_length=160,required=False,
        widget=forms.TextInput(attrs={'placeholder':'Необязательно — используем название первого файла'}))


@transaction.atomic
def start(user,batch_id,directions,set_ids,experience,key,*,logging_enabled=False):
    batch=Batch.objects.select_for_update().get(pk=batch_id)
    if not can_edit(user,batch) or batch.archived:raise PermissionDenied('Batch owner required')
    if not directions or len(set(directions))!=len(directions) or set(directions)-{x[0] for x in CHECKS}:
        raise ValueError('Выберите направления проверки.')
    previous=Command.objects.filter(idempotency_key=digest(['check-start',user.pk,str(key)])).first()
    if previous:
        if (previous.payload.get('batch_id')!=str(batch.pk) or previous.payload.get('set_ids')!=sorted(set_ids)
            or previous.payload.get('directions')!=directions or previous.payload.get('experience_set_id')!=(experience or None)
            or bool(previous.payload.get('logging',{}).get('enabled'))!=logging_enabled):
            raise Conflict('Повтор запуска отличается от первоначального запроса.')
        return KnowledgeCheck.objects.get(pk=previous.payload['job_id'])
    run=WorkerRun.objects.filter(batch=batch).first()
    # A running legacy review may gain a normative stage, but is never reset or reconfigured.
    attach=bool(run)
    if attach:
        if logging_enabled!=batch.logging_enabled:raise Conflict('Для уже идущей проверки нельзя изменить запись журнала. Создайте повторную проверку с нужной опцией.')
        if 'sto' not in directions or set(directions)-{'sto'}!=set(batch.checks)-{'sto'}:
            raise Conflict('Идущую проверку нельзя перенастроить. Можно добавить только этап СТО.')
    elif batch.status not in ('prepared','cancelled'):
        raise Conflict('Пакет уже запущен. Обновите страницу.')
    legacy=[x for x in directions if x!='sto']
    if type(logging_enabled) is not bool:raise ValueError('Logging boolean')
    batch.logging_enabled=logging_enabled;batch.save(update_fields=['logging_enabled'])
    job=None
    if 'sto' in directions:
        from portal.models import WorkerPresence
        from django.utils import timezone
        from datetime import timedelta
        pipeline=bool(legacy and not run and WorkerPresence.objects.filter(details__pipeline_version='pipeline-v1',state__in=['idle','busy'],heartbeat__gte=timezone.now()-timedelta(seconds=90)).exists())
        job,c=checks.start(user,batch.pk,set_ids,experience or None,str(key),workflow={
            'unified_launch':True,'after_non_normative':bool(legacy or run),'directions':list(directions),
            **({'pipeline_version':'pipeline-v1'} if pipeline else {})})
    batch.checks=list(directions)
    if not attach:
        batch.status='waiting' if legacy else 'running'
        batch.queue_position=(Batch.objects.filter(status='waiting').aggregate(n=Max('queue_position'))['n'] or 0)+1 if legacy else 0
    elif run.state in ('completed','partial'):
        batch.status='running'
    batch.save(update_fields=['checks','status','queue_position'])
    return job


def dependency_ready(command):
    """Polling never consumes attempts while the preceding executor is busy."""
    if not command.payload.get('unified_launch'):return True
    batch=Batch.objects.get(pk=command.payload['batch_id'])
    job=KnowledgeCheck.objects.get(pk=command.payload['job_id'])
    if batch.status=='cancelled':
        job.state='paused';job.pause_requested=True
        job.summary={'reason':'Пакет отменён. Нормативный этап не запускался.'}
        job.save(update_fields=['state','pause_requested','summary'])
        command.state='done';command.result={'reason':'batch_cancelled'}
        command.save(update_fields=['state','result'])
        return False
    if job.pause_requested or job.state=='paused':return False
    if command.payload.get('pipeline_version')=='pipeline-v1':return batch.status not in ('paused','failed')
    if command.payload.get('after_non_normative'):
        run=WorkerRun.objects.filter(batch=batch).first()
        if not run or run.state not in ('completed','partial','failed','cancelled'):return False
        if run.state in ('failed','cancelled'):
            job.state='failed';job.summary={'reason':'Предшествующие этапы остановлены с ошибкой или отменены. Нормативный этап не запускался.'}
            job.save(update_fields=['state','summary'])
            command.state='failed';command.result=job.summary;command.save(update_fields=['state','result'])
            reconcile(batch)
            return False
    # Both executors use the same local model. SQLite reserves the writer for claim.
    return not WorkerRun.objects.filter(state__in=['claimed','preparing','running']).exists()


def reconcile(batch):
    """Only packages explicitly launched together receive an aggregate status."""
    command=Command.objects.filter(kind='review.execute',payload__batch_id=str(batch.pk),payload__unified_launch=True).order_by('-created').first()
    if not command or batch.status=='cancelled':return
    job=KnowledgeCheck.objects.get(pk=command.payload['job_id'])
    run=WorkerRun.objects.filter(batch=batch).first()
    if run and run.state not in ('completed','partial','failed','cancelled'):state=run.state if run.state!='claimed' else 'waiting'
    elif job.state in ('queued','running'):state='running'
    elif job.state=='paused':state='paused'
    elif job.state=='failed':state='failed'
    else:state='partial' if job.state=='partial' or (run and run.state!='completed') else 'completed'
    Batch.objects.filter(pk=batch.pk).update(status=state)
