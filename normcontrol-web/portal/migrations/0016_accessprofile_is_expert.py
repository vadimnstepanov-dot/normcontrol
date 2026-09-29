from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0015_batchlogchunk')]
    operations = [migrations.AddField(model_name='accessprofile', name='is_expert',
                                     field=models.BooleanField(default=False))]
