"""Explicit lifecycle controls, independent of expert confidence and canonical card versions."""
from django.db import transaction
from django.http import JsonResponse
from .models import ObjectControl,NormativeSet,DocumentProfile,ExpertCard,SourceUpload,NormativeLink,Command
from .access import require
from .services import audit,Conflict
from .views import boundary,fields

def metadata(kind,identity):
    c=ObjectControl.objects.filter(kind=kind,object_id=identity).first()
    return dict(enabled=c.enabled if c else True,deleted=c.deleted if c else False,revision=c.revision if c else 0)

def excluded(dataset,kind):
    return set(map(str,ObjectControl.objects.filter(normative_set=dataset,kind=kind).filter(enabled=False).values_list('object_id',flat=True)))

def disabled_profiles(dataset):
    profiles=list(DocumentProfile.objects.filter(scope=dataset.scope,archived=False))
    blocked=excluded(dataset,'profile')
    while True:
        children={str(p.pk) for p in profiles if set(p.definition.get('parents',[]))&blocked}
        new=blocked|children
        if new==blocked:return blocked
        blocked=new

def selected_cards(dataset,rows):
    sources=excluded(dataset,'source');cards=excluded(dataset,'card');blocked=disabled_profiles(dataset)
    bindings={(b['source_id'],b['profile_id']) for p in DocumentProfile.objects.filter(scope=dataset.scope) if str(p.pk) in blocked for b in p.definition.get('bindings',[])}
    return [c for c in rows if str(c.pk) not in cards and str(c.source_id) not in sources and c.payload.get('local_profile') not in blocked and (str(c.source_id),c.profile_key) not in bindings]

@boundary({'POST'})
@transaction.atomic
def change(request,identity,kind,object_id):
    dataset=NormativeSet.objects.select_for_update().get(pk=identity)
    require(request.user,dataset.scope,'review')
    d=fields(request,{'action','expected_revision','reason'},set())
    if d['action'] not in ('activate','deactivate','delete') or not isinstance(d['reason'],str) or not 5<=len(d['reason'].strip())<=2000:raise ValueError('Lifecycle decision')
    if kind=='area':obj=NormativeSet.objects.get(pk=object_id,pk__in=[dataset.pk])
    elif kind=='source':obj=SourceUpload.objects.get(pk=object_id,normative_set=dataset)
    elif kind=='profile':obj=DocumentProfile.objects.get(pk=object_id,normative_set=dataset,archived=False)
    elif kind=='card':obj=ExpertCard.objects.get(pk=object_id,source__normative_set=dataset,latest_analysis=True)
    elif kind=='link':obj=NormativeLink.objects.get(pk=object_id,normative_set=dataset)
    else:raise ValueError('Object kind')
    if Command.objects.filter(normative_set=dataset,state__in=['pending','delivering']).exclude(kind__in=['review.execute','normative.search']).exists():raise Conflict('Дождитесь завершения обработки или публикации области.')
    row=ObjectControl.objects.select_for_update().filter(kind=kind,object_id=object_id).first()
    if d['expected_revision']!=(row.revision if row else 0):raise Conflict('Object state changed')
    if row and row.deleted:raise Conflict('Удалённый объект сохранён в истории; восстановление требует отдельного решения.')
    row=row or ObjectControl(normative_set=dataset,kind=kind,object_id=object_id,revision=0)
    row.enabled=d['action']=='activate';row.deleted=d['action']=='delete';row.revision+=1;row.save()
    dataset.metadata_revision+=1
    # New launches must wait for a release reflecting this selection. Pinned runs are untouched.
    dataset.state='editing';dataset.save(update_fields=['state','metadata_revision'])
    audit(request.user,'object.'+d['action'],object_id,dict(area=str(identity),kind=kind,revision=row.revision,reason=d['reason']))
    if kind in ('card','source'):
        from .glossary import sync
        sync(dataset)
    from .automatic import advance
    advance(dataset,request.user)
    return JsonResponse(dict(control=metadata(kind,object_id),publication='Обновление состава области; ранее запущенные проверки не изменены.'))
