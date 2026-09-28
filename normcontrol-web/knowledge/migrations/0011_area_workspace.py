from django.db import migrations, models
import django.db.models.deletion


def assign_profiles(apps, schema_editor):
    Profile = apps.get_model('knowledge', 'DocumentProfile')
    Source = apps.get_model('knowledge', 'SourceUpload')
    for profile in Profile.objects.iterator():
        sources = [b.get('source_id') for b in profile.definition.get('bindings', [])]
        sets = list(Source.objects.filter(pk__in=sources).values_list('normative_set_id', flat=True).distinct())
        if len(sets) == 1:
            profile.normative_set_id = sets[0]
            profile.save(update_fields=['normative_set'])


class Migration(migrations.Migration):
    dependencies = [('knowledge', '0010_normativelink')]
    operations = [
        migrations.AddField('normativeset', 'description', models.TextField(blank=True)),
        migrations.AddField('normativeset', 'automatic', models.BooleanField(default=False)),
        migrations.AddField('documentprofile', 'normative_set', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='knowledge.normativeset')),
        migrations.AddField('documentprofile', 'archived', models.BooleanField(default=False)),
        migrations.RunPython(assign_profiles, migrations.RunPython.noop),
    ]
