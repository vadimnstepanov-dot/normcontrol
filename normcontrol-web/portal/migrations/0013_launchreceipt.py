from django.conf import settings
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('portal','0012_llmruntime'),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[migrations.CreateModel(name='LaunchReceipt',fields=[
        ('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),
        ('key',models.UUIDField()),('fingerprint',models.CharField(max_length=64)),
        ('batch',models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,to='portal.batch')),
        ('user',models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,to=settings.AUTH_USER_MODEL)),
    ],options={'constraints':[models.UniqueConstraint(fields=('user','key'),name='unique_user_launch_key')]})]
