import uuid
from django.db import migrations,models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies=[('portal','0020_alter_conversation_batch_and_more')]
    operations=[migrations.CreateModel(name='ChatResponse',fields=[
        ('id',models.UUIDField(default=uuid.uuid4,editable=False,primary_key=True,serialize=False)),
        ('state',models.CharField(db_index=True,default='queued',max_length=12)),
        ('lease',models.UUIDField(blank=True,null=True)),
        ('lease_until',models.DateTimeField(blank=True,null=True)),
        ('created',models.DateTimeField(auto_now_add=True)),('updated',models.DateTimeField(auto_now=True)),
        ('assistant_message',models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name='model_request',to='portal.chatmessage')),
        ('user_message',models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name='model_response',to='portal.chatmessage')),
    ])]
