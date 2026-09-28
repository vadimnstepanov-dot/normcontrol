from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('portal','0014_batch_logging')]
    operations=[migrations.CreateModel(name='BatchLogChunk',fields=[('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('sequence',models.PositiveIntegerField()),('entries',models.JSONField()),('digest',models.CharField(max_length=64)),('created',models.DateTimeField(auto_now_add=True)),('batch',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='log_chunks',to='portal.batch'))],options={'constraints':[models.UniqueConstraint(fields=('batch','sequence'),name='batch_log_sequence')]})]
