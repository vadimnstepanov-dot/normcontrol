from django.db import migrations,models
import django.db.models.deletion
class Migration(migrations.Migration):
    dependencies=[('knowledge','0014_source_identity')]
    operations=[migrations.CreateModel(name='ObjectControl',fields=[('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('kind',models.CharField(max_length=16)),('object_id',models.UUIDField()),('enabled',models.BooleanField(default=True)),('deleted',models.BooleanField(default=False)),('revision',models.PositiveIntegerField(default=1)),('normative_set',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='knowledge.normativeset'))],options={'constraints':[models.UniqueConstraint(fields=('kind','object_id'),name='knowledge_object_control')]})]
