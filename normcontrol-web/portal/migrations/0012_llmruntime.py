from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[('portal','0011_accessprofile_can_view_others')]
    operations=[migrations.CreateModel(name='LLMRuntime',fields=[
        ('id',models.PositiveSmallIntegerField(default=1,primary_key=True,serialize=False)),
        ('sample',models.JSONField(default=dict)),
        ('history',models.JSONField(default=list)),
        ('command',models.JSONField(default=dict)),
        ('updated',models.DateTimeField(auto_now=True)),
    ])]
