"""Source requisites are editable metadata, never substitutes for original evidence."""
import copy
from django.db import transaction
from django.http import JsonResponse
from .models import SourceUpload,AuditEvent,NormativeSet
from .access import require
from .services import audit,Conflict,digest,command,NotReady
from .views import boundary,fields
from knowledge_v2.source_identity import FIELDS,VERSION


def project(source,value,actor):
    if not value or value.get('source_sha256')!=source.sha256 or set(value.get('fields',{}))!=set(FIELDS):return
    if any(not isinstance(v,dict) or not isinstance(v.get('value'),str) or len(v['value'])>2000 for v in value['fields'].values()):raise ValueError('Source identity fields')
    previous=source.identification;result=copy.deepcopy(value)
    manual=previous.get('manual_fields',[])
    for key in manual:result['fields'][key]=previous['fields'][key]
    result['manual_fields']=manual
    if digest(previous)==digest(result):return
    source.identification=result;source.identification_revision+=1;source.save(update_fields=['identification','identification_revision'])
    audit(actor,'source.identity.extracted',source.pk,dict(revision=source.identification_revision,previous=previous,current=result))


@boundary({'POST'})
def edit(request,set_id,source_id):
    d=fields(request,{'expected_revision','fields','reason'})
    if not isinstance(d['fields'],dict) or not d['fields'] or not set(d['fields'])<=set(FIELDS) or any(not isinstance(v,str) or len(v)>2000 for v in d['fields'].values()):raise ValueError('Source identity edit')
    if not isinstance(d['reason'],str) or not 10<=len(d['reason'].strip())<=4000:raise ValueError('Explain source metadata edit')
    with transaction.atomic():
        area=NormativeSet.objects.select_for_update().select_related('scope').get(pk=set_id);require(request.user,area.scope,'review')
        source=SourceUpload.objects.select_for_update().get(pk=source_id,normative_set=area)
        if type(d['expected_revision']) is not int or source.identification_revision!=d['expected_revision']:raise Conflict('Source identity changed')
        previous=copy.deepcopy(source.identification)
        value=copy.deepcopy(previous) if previous else dict(version=VERSION,status='expert_edited',source_sha256=source.sha256,fields={k:dict(value='',citations=[]) for k in FIELDS},confidence=None,issues=[])
        manual=set(value.get('manual_fields',[]))
        for key,text in d['fields'].items():
            if text.strip()!=value['fields'].get(key,{}).get('value',''):
                value['fields'][key]=dict(value=text.strip(),citations=[],origin='expert');manual.add(key)
        value.update(manual_fields=sorted(manual),status='expert_edited')
        source.identification=value;source.identification_revision+=1;source.save(update_fields=['identification','identification_revision'])
        area.metadata_revision+=1;area.save(update_fields=['metadata_revision'])
        audit(request.user,'source.identity.edited',source.pk,dict(revision=source.identification_revision,reason=d['reason'],previous=previous,current=value))
    return JsonResponse(dict(id=str(source.pk),name=source.display_name,identification=value,identification_revision=source.identification_revision))


def history(source):
    return [dict(date=e.created.isoformat(),author=(e.actor.get_full_name() or e.actor.username) if e.actor else 'Система',action=e.action,revision=e.data.get('revision'),reason=e.data.get('reason','Автоматическое извлечение'),fields=e.data.get('current',{}).get('fields',{})) for e in AuditEvent.objects.filter(aggregate_id=str(source.pk),action__in=['source.identity.extracted','source.identity.edited']).select_related('actor').order_by('-created')[:30]]


@boundary({'POST'})
def identify(request,set_id,source_id):
    fields(request,set())
    source=SourceUpload.objects.select_related('normative_set__scope').get(pk=source_id,normative_set_id=set_id)
    require(request.user,source.normative_set.scope,'upload')
    if source.state not in ('prepared','partial'):raise NotReady('Source not parsed')
    key=request.headers.get('Idempotency-Key')
    if not key:raise ValueError('Idempotency key required')
    c=command(request.user,source.normative_set,'source.identify',digest(['source-identify',request.user.pk,key]),dict(set_id=str(set_id),source_id=str(source_id),sha256=source.sha256))
    return JsonResponse(dict(command_id=str(c.pk),state=c.state),status=202)
