from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('portal','0006_workerpresence')]
    operations=[migrations.AddField(model_name='batch',name='source_batch',field=models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.SET_NULL,related_name='reruns',to='portal.batch')),
                migrations.AddField(model_name='batch',name='fresh_review',field=models.BooleanField(default=False))]
