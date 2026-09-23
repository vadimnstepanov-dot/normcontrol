from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0010_workerpresence_details')]

    operations = [
        migrations.AddField(
            model_name='accessprofile',
            name='can_view_others',
            field=models.BooleanField(default=False),
        ),
    ]
