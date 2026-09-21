from django.conf import settings
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('portal','0008_batch_queue_position'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[
        migrations.CreateModel(
            name='FindingDisposition',
            fields=[
                ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
                ('finding_id',models.CharField(max_length=64)),
                ('state',models.CharField(choices=[('new','Не рассмотрено'),('in_work','В работе'),('fixed','Исправлено'),('disputed','Не согласен')],default='new',max_length=16)),
                ('comment',models.TextField(blank=True)),
                ('updated',models.DateTimeField(auto_now=True)),
                ('author',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to=settings.AUTH_USER_MODEL)),
                ('batch',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name='finding_dispositions',to='portal.batch')),
            ],
            options={'constraints':[models.UniqueConstraint(fields=('batch','finding_id'),name='unique_batch_finding_disposition')]},
        ),
    ]
