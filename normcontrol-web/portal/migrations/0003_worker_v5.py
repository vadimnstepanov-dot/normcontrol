from django.db import migrations,models
from django.conf import settings
import django.db.models.deletion
import uuid

class Migration(migrations.Migration):
    dependencies=[('portal','0001_initial'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[
        migrations.CreateModel(name='WorkerRun',fields=[('id',models.BigAutoField(primary_key=True,serialize=False)),('lease',models.UUIDField(default=uuid.uuid4,unique=True)),('worker',models.CharField(max_length=100)),('local_id',models.CharField(max_length=64,blank=True)),('state',models.CharField(max_length=24,default='claimed')),('sequence',models.PositiveBigIntegerField(default=0)),('snapshot',models.JSONField(default=dict)),('report',models.JSONField(default=dict)),('heartbeat',models.DateTimeField(auto_now=True)),('batch',models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name='worker_run',to='portal.batch'))]),
        migrations.CreateModel(name='ReviewFeedback',fields=[('id',models.BigAutoField(primary_key=True,serialize=False)),('finding_id',models.CharField(max_length=64)),('comment',models.TextField()),('state',models.CharField(max_length=24,default='pending')),('decision',models.JSONField(default=dict)),('created',models.DateTimeField(auto_now_add=True)),('batch',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='review_feedback',to='portal.batch')),('author',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL))])]
