from django.db import migrations,models

class Migration(migrations.Migration):
    dependencies=[('portal','0009_findingdisposition')]
    operations=[migrations.AddField(model_name='workerpresence',name='details',field=models.JSONField(default=dict))]
