import uuid
from django.conf import settings
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('knowledge','0009_expert_workspace'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[migrations.CreateModel(name='NormativeLink',fields=[
        ('id',models.UUIDField(default=uuid.uuid4,editable=False,primary_key=True,serialize=False)),
        ('revision',models.PositiveIntegerField(default=1)),('payload',models.JSONField()),('updated',models.DateTimeField(auto_now=True)),
        ('normative_set',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='trace_links',to='knowledge.normativeset'))]),
        migrations.CreateModel(name='NormativeLinkRevision',fields=[('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
        ('revision',models.PositiveIntegerField()),('payload',models.JSONField()),('digest',models.CharField(max_length=64)),
        ('request_key',models.CharField(max_length=64,unique=True)),('intent_digest',models.CharField(max_length=64)),
        ('created',models.DateTimeField(auto_now_add=True)),
        ('actor',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
        ('link',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='history',to='knowledge.normativelink'))],
        options={'constraints':[models.UniqueConstraint(fields=('link','revision'),name='trace_link_revision')]})]
