import uuid
from django.db import models
from django.contrib.auth.models import User

class AccessProfile(models.Model):
    user=models.OneToOneField(User,on_delete=models.CASCADE)
    must_change_password=models.BooleanField(default=False)

class Batch(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    owner=models.ForeignKey(User,on_delete=models.PROTECT)
    name=models.CharField(max_length=160)
    profile=models.CharField(max_length=12,choices=[('chtz','Частное техническое задание'),('tz','Техническое задание'),('oit','Описание информационной технологии'),('other','Другой технический документ')],default='chtz')
    checks=models.JSONField(default=list)
    status=models.CharField(max_length=20,choices=[('prepared','Подготовлен'),('waiting','В очереди'),('preparing','Подготовка'),('running','Проверяется'),('paused','Приостановлен'),('partial','Завершён с ограничениями'),('completed','Завершён'),('failed','Ошибка'),('cancelled','Отменён')],default='prepared')
    created=models.DateTimeField(auto_now_add=True)
    archived=models.BooleanField(default=False)
    source_batch=models.ForeignKey('self',null=True,blank=True,on_delete=models.SET_NULL,related_name='reruns')
    fresh_review=models.BooleanField(default=False)
    queue_position=models.BigIntegerField(default=0,db_index=True)
    @property
    def can_rerun(self):return self.status in ('completed','partial','failed','cancelled')
    class Meta:ordering=['-created']

def private_path(instance,filename):return str(instance.batch_id)+'/'+uuid.uuid4().hex+'.docx'

class Document(models.Model):
    batch=models.ForeignKey(Batch,on_delete=models.CASCADE,related_name='documents')
    name=models.CharField(max_length=240)
    file=models.FileField(upload_to=private_path)
    size=models.PositiveBigIntegerField()
    sha256=models.CharField(max_length=64)
    paragraphs=models.PositiveIntegerField(default=0)
    tables=models.PositiveIntegerField(default=0)
    outline=models.JSONField(default=list)
    @property
    def detected_type(self):
        name=self.name.casefold()
        if 'чтз' in name:return 'Частное техническое задание'
        if 'оит' in name:return 'Описание информационной технологии'
        if 'тз' in name:return 'Техническое задание'
        return 'Технический документ — тип будет уточнён по структуре'

class LLMConfig(models.Model):
    name=models.CharField(max_length=80,default='Основная модель')
    endpoint=models.URLField(blank=True,max_length=500)
    model=models.CharField(max_length=160,blank=True)
    key_encrypted=models.TextField(blank=True)
    context_tokens=models.PositiveIntegerField(default=20480)
    output_tokens=models.PositiveIntegerField(default=6144)
    concurrency=models.PositiveSmallIntegerField(default=1)
    timeout_seconds=models.PositiveIntegerField(default=300)
    updated=models.DateTimeField(auto_now=True)
    last_probe=models.JSONField(default=dict)

class Audit(models.Model):
    user=models.ForeignKey(User,on_delete=models.SET_NULL,null=True)
    action=models.CharField(max_length=240)
    created=models.DateTimeField(auto_now_add=True)
    class Meta:ordering=['-created']

class LoginAttempt(models.Model):
    identity=models.CharField(max_length=64,db_index=True)
    ip=models.CharField(max_length=64,db_index=True)
    created=models.DateTimeField(auto_now_add=True,db_index=True)

class WorkerRun(models.Model):
    batch=models.OneToOneField(Batch,on_delete=models.CASCADE,related_name='worker_run')
    lease=models.UUIDField(default=uuid.uuid4,unique=True)
    worker=models.CharField(max_length=100)
    local_id=models.CharField(max_length=64,blank=True)
    state=models.CharField(max_length=24,default='claimed')
    sequence=models.PositiveBigIntegerField(default=0)
    snapshot=models.JSONField(default=dict)
    report=models.JSONField(default=dict)
    heartbeat=models.DateTimeField(auto_now=True)
    control=models.CharField(max_length=16,blank=True)

class ReviewFeedback(models.Model):
    batch=models.ForeignKey(Batch,on_delete=models.CASCADE,related_name='review_feedback')
    author=models.ForeignKey(User,on_delete=models.PROTECT)
    finding_id=models.CharField(max_length=64)
    comment=models.TextField()
    state=models.CharField(max_length=24,default='pending')
    decision=models.JSONField(default=dict)
    created=models.DateTimeField(auto_now_add=True)

class FindingDisposition(models.Model):
    STATES=[('new','Не рассмотрено'),('in_work','В работе'),('fixed','Исправлено'),('disputed','Не согласен')]
    batch=models.ForeignKey(Batch,on_delete=models.CASCADE,related_name='finding_dispositions')
    finding_id=models.CharField(max_length=64)
    author=models.ForeignKey(User,on_delete=models.PROTECT)
    state=models.CharField(max_length=16,choices=STATES,default='new')
    comment=models.TextField(blank=True)
    updated=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['batch','finding_id'],name='unique_batch_finding_disposition')]

class WorkerPresence(models.Model):
    name=models.CharField(max_length=100,primary_key=True)
    state=models.CharField(max_length=16,default='idle')
    heartbeat=models.DateTimeField(auto_now=True)
