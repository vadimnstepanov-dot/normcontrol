from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('knowledge','0011_area_workspace')]
    operations=[migrations.CreateModel(name='CheckLogChunk',fields=[
        ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
        ('sequence',models.PositiveIntegerField()),('entries',models.JSONField()),('digest',models.CharField(max_length=64)),
        ('created',models.DateTimeField(auto_now_add=True)),
        ('job',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,related_name='log_chunks',to='knowledge.knowledgecheck'))],
        options={'constraints':[models.UniqueConstraint(fields=('job','sequence'),name='knowledge_check_log_chunk')]})]
