from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies=[('portal','0013_launchreceipt')]
    operations=[migrations.AddField('batch','logging_enabled',models.BooleanField(default=False))]
