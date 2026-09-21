from django.db import migrations,models


def populate_queue(apps,schema_editor):
    Batch=apps.get_model('portal','Batch')
    rows=Batch.objects.filter(status='waiting',archived=False).order_by('created','pk').values_list('pk',flat=True)
    for position,pk in enumerate(rows,1):Batch.objects.filter(pk=pk).update(queue_position=position)


class Migration(migrations.Migration):
    dependencies=[('portal','0007_batch_source_batch')]
    operations=[
        migrations.AddField(model_name='batch',name='queue_position',field=models.BigIntegerField(db_index=True,default=0)),
        migrations.RunPython(populate_queue,migrations.RunPython.noop),
    ]
