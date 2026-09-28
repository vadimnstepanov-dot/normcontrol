"""JSON import/export through a source-verified, replay-safe canonical command."""
import copy,json,uuid
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from knowledge_v2.portable_area import FORMAT,VERSION,validate,remap,LIMIT
from .models import NormativeSet,ExpertCard,ExpertCardRevision,DocumentProfile,SourceUpload,Command,AnalysisChunk,GlossaryGroup,NormativeLinkRevision
from .access import require
from .views import boundary,fields
from .services import digest,command,Conflict,audit


@boundary({'GET'})
def export(request,identity):
    area=NormativeSet.objects.select_related('scope').get(pk=identity);require(request.user,area.scope,'read')
    cards=list(ExpertCard.objects.filter(source__normative_set=area,latest_analysis=True).exclude(status__in=['superseded','rejected']).select_related('source'))
    # Older expert projections store these two indexed fields outside payload.
    rows=[dict(id=str(c.pk),source_id=str(c.source_id),payload=dict(c.payload,
        entity_type=c.payload.get('entity_type') or c.entity_type,
        description=c.payload.get('description') or c.description)) for c in cards]
    from .glossary import frozen
    data=dict(format=FORMAT,version=VERSION,exported_at=timezone.now().isoformat(),area=dict(name=area.name,description=area.description,automatic=area.automatic),
        sources=[dict(id=str(s.pk),filename=s.filename,sha256=s.sha256,metadata={k:v.get('value','') for k,v in s.identification.get('fields',{}).items()}) for s in area.sources.all()],
        profiles=[dict(id=str(p.pk),definition=p.definition) for p in area.profiles.filter(archived=False)],
        requirements=[r for r in rows if r['payload'].get('entity_type')!='definition'],
        glossary=dict(entries=[r for r in rows if r['payload'].get('entity_type')=='definition'],choices=[dict(key=c['key'],active_entry_id=c['active']) for c in frozen(area,cards)]),
        interdocument_requirements=[l.payload for l in area.trace_links.exclude(payload__status='rejected') if all(l.payload.get(k) in {str(c.pk) for c in cards} for k in ('source','target','basis'))])
    from .models import ObjectControl
    known=dict(source={s['id'] for s in data['sources']},profile={p['id'] for p in data['profiles']},card={r['id'] for r in rows},link={l['id'] for l in data['interdocument_requirements']},area={str(area.pk)})
    data['controls']=[dict(kind=c.kind,id='area' if c.kind=='area' else str(c.object_id),enabled=c.enabled,deleted=c.deleted) for c in ObjectControl.objects.filter(normative_set=area) if str(c.object_id) in known.get(c.kind,set())]
    validate(data)
    response=JsonResponse(data,json_dumps_params={'ensure_ascii':False,'indent':2});response['Content-Disposition']='attachment; filename="normative-area.json"';response['Cache-Control']='private, no-store';return response


def plan(area,document,identity):
    validate(document)
    profiles=list(area.profiles.filter(archived=False))
    # Import replaces generated candidates only, never somebody's existing expert work.
    if any(p.revision>1 and 'глоссар' not in p.name.casefold() for p in profiles) or ExpertCard.objects.filter(source__normative_set=area).filter(revision__gt=1).exists() or ExpertCardRevision.objects.filter(card__source__normative_set=area,action='create').exists():raise Conflict('В области есть экспертные правки. Импортируйте JSON в новую область; существующие решения не перезаписываются.')
    if Command.objects.filter(normative_set=area,state__in=['pending','delivering']).exclude(kind__in=['review.execute','normative.search']).exists():raise Conflict('Дождитесь завершения обработки нормативных документов.')
    sources={s.sha256:s for s in area.sources.all()};missing=[s for s in document['sources'] if s['sha256'] not in sources]
    if missing:return dict(ready=False,missing_sources=[dict(filename=s['filename'],sha256=s['sha256']) for s in missing])
    if set(sources)!={s['sha256'] for s in document['sources']}:raise Conflict('В целевой области есть дополнительные оригиналы. Используйте новую область с точным составом источников JSON.')
    source_map={s['id']:sources[s['sha256']] for s in document['sources']}
    if any(s.state not in ('prepared','partial') for s in source_map.values()):raise Conflict('Дождитесь завершения чтения оригиналов перед импортом JSON.')
    ids={old:str(s.pk) for old,s in source_map.items()}
    ids.update({p['id']:str(uuid.uuid5(identity,'profile:'+p['id'])) for p in document['profiles']})
    records=document['requirements']+document['glossary']['entries'];ids.update({c['id']:str(uuid.uuid5(identity,'card:'+c['id'])) for c in records})
    candidates=list(ExpertCard.objects.filter(source__in=list(source_map.values()),latest_analysis=True,revision=1).select_related('source','analysis'))
    by_citation={}
    for c in candidates:
        for proof in c.payload.get('citations',[]):by_citation.setdefault((str(c.source_id),proof.get('locator'),proof.get('quote')),set()).add(str(c.base_id))
    imports=[];keys={};direct=set()
    for c in records:
        source=source_map[c['source_id']]
        matches=[candidate for candidate in candidates if candidate.source_id==source.pk and any((p.get('locator'),p.get('quote'))==(q.get('locator'),q.get('quote')) for p in candidate.payload.get('citations',[]) for q in c['payload']['citations'])]
        mapped={m.profile_key for m in matches}
        if len(mapped)!=1:
            # A clean area has no generated obligations. Do not require an LLM to
            # regenerate a human-prepared set merely to establish its provenance.
            # The canonical worker verifies the original and EVERY nested quote.
            if any(candidate.source_id==source.pk for candidate in candidates) and c['payload'].get('preparation_origin')!='independent_source_analysis':
                raise ValueError('Нельзя однозначно сопоставить нормативный профиль по цитатам. Требуется сверка оригинала.')
            direct.add(c['id']);continue
        key=(c['source_id'],str(c['payload'].get('profile_id') or ''))
        if key in keys and keys[key]!=next(iter(mapped)):raise ValueError('Conflicting original profile mapping')
        keys[key]=next(iter(mapped))
    for p in document['profiles']:
        for binding in p['definition']['bindings']:
            key=(binding['source_id'],str(binding['profile_id'] or ''))
            if key in keys:continue
            source=source_map[binding['source_id']]
            available={row['id']:row for a in Command.objects.filter(kind='source.analyze',state='done',normative_set=area,payload__source_id=str(source.pk)) for row in a.result.get('summary',{}).get('profiles',[])}
            if binding['profile_id'] in available:keys[key]=binding['profile_id'];continue
            matches=[row['id'] for row in available.values() if row['name']==p['definition']['name']]
            if len(matches)!=1:
                if any(c['id'] in direct for c in records if c['source_id']==binding['source_id']):
                    keys[key]=ids[p['id']];continue
                raise ValueError('Не сопоставлен профиль без требований: '+p['definition']['name'])
            keys[key]=matches[0]
    def profile_key(old_source,key):return keys.get((old_source,str(key or '')),str(key or ''))
    for c in records:
        source=source_map[c['source_id']];p=remap(copy.deepcopy(c['payload']),ids);bases=set()
        for citation in p['citations']:bases.update(by_citation.get((str(source.pk),citation.get('locator'),citation.get('quote')),set()))
        if c['id'] in direct:
            if p['entity_type']=='definition':
                # Glossary is an area-level entity, not a document profile.
                p.pop('local_profile',None);key='area-glossary';p['profile_ids']=[]
            else:
                key=p.get('local_profile') or p.get('profile_id')
                if key not in {ids[x['id']] for x in document['profiles']}:raise ValueError('Импортируемое требование должно принадлежать профилю JSON.')
                p['profile_ids']=[key]
            p['profile_id']=key;p['effective_profile_id']=key
            bases={str(uuid.uuid5(identity,'source-evidence:'+c['id']))}
        elif not bases:raise ValueError('Нормативное основание не найдено в обработанном оригинале: '+c['payload']['description'][:150])
        else:
            p['profile_id']=profile_key(c['source_id'],c['payload'].get('profile_id',''));p['effective_profile_id']=p['profile_id']
        imports.append(dict(id=ids[c['id']],source_id=str(source.pk),sha256=source.sha256,card=p,base_ids=sorted(bases),**({'evidence_mode':'source_fragments'} if c['id'] in direct else {})))
    imported_profiles=[]
    for p in document['profiles']:
        d=remap(copy.deepcopy(p['definition']),ids)
        d['bindings']=[dict(source_id=ids[b['source_id']],profile_id=profile_key(b['source_id'],b['profile_id'])) for b in p['definition']['bindings']]
        if direct and not d['bindings']:
            d['bindings']=[dict(source_id=ids[s['id']],profile_id=ids[p['id']]) for s in document['sources']]
        imported_profiles.append(dict(id=ids[p['id']],definition=d))
    return dict(ready=True,controls=remap(document.get('controls',[]),ids),profiles=imported_profiles,cards=imports,choices=remap(document['glossary']['choices'],ids),links=remap(document['interdocument_requirements'],ids),area=document['area'],source_metadata=[dict(id=ids[s['id']],fields=s.get('metadata',{}),revision=source_map[s['id']].identification_revision) for s in document['sources']],
        replace_cards=[dict(id=str(c.pk),revision=c.revision) for c in candidates],replace_profiles=[dict(id=str(p.pk),revision=p.revision) for p in profiles],
        stats=dict(profiles=len(imported_profiles),requirements=len(document['requirements']),glossary=len(document['glossary']['entries']),links=len(document['interdocument_requirements'])))


@boundary({'POST'},max_body=LIMIT+65536)
def load(request,identity):
    area=NormativeSet.objects.select_related('scope').get(pk=identity);require(request.user,area.scope,'review');require(request.user,area.scope,'upload')
    if len(request.body)>LIMIT:raise ValueError('JSON area exceeds 32 MiB')
    d=fields(request,{'document','apply'},{'expected_revision'})
    if type(d['apply']) is not bool:raise ValueError('Import confirmation')
    key=request.headers.get('Idempotency-Key')
    if not key:raise ValueError('Import idempotency key required')
    cid=uuid.uuid5(area.pk,digest(['area-import',request.user.pk,key,digest(d['document'])]))
    with transaction.atomic():
        area=NormativeSet.objects.select_for_update().get(pk=identity)
        require(request.user,area.scope,'review');require(request.user,area.scope,'upload')
        existing=Command.objects.filter(idempotency_key=digest(['area-import',str(cid)]),actor=request.user,normative_set=area).first()
        if existing:return JsonResponse(dict(command_id=str(existing.pk),state=existing.state),status=202)
        try:prepared=plan(area,d['document'],cid)
        except (ValueError,Conflict) as e:return JsonResponse(dict(error=str(e)),status=409 if isinstance(e,Conflict) else 400)
        if not prepared['ready']:return JsonResponse(prepared,status=409)
        if not d['apply']:return JsonResponse(dict(ready=True,stats=prepared['stats'],revision=area.metadata_revision,warnings=['Экспертные отметки сбрасываются; импортированные записи требуют сверки с первоисточниками.','Все цитаты проверит локальный обработчик по оригиналам перед применением; предварительный просмотр не подтверждает их происхождение.','Автоматически извлечённые профили и кандидаты будут заменены. История и запущенные проверки сохранятся.']))
        if d.get('expected_revision')!=area.metadata_revision:raise Conflict('Area changed before import')
        payload=dict(set_id=str(area.pk),actor_id=request.user.pk,**prepared,area_revision=area.metadata_revision)
        c=command(request.user,area,'area.import',digest(['area-import',str(cid)]),payload)
        # Command UUID is assigned by the existing service; imported IDs were already safely namespaced above.
        return JsonResponse(dict(command_id=str(c.pk),state=c.state),status=202)


@transaction.atomic
def receive(worker_id,command_id,lease,sequence,entries,entry_hash):
    if type(sequence) is not int or sequence<0 or not isinstance(entries,list) or not 1<=len(entries)<=100 or digest(entries)!=entry_hash:raise ValueError('Import chunk')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='area.import')
    if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Import lease')
    require(c.actor,c.normative_set.scope,'review')
    require(c.actor,c.normative_set.scope,'upload')
    chunk,new=AnalysisChunk.objects.get_or_create(command=c,sequence=sequence,defaults=dict(entries=entries,digest=entry_hash))
    if not new and chunk.digest!=entry_hash:raise Conflict('Import replay changed')
    return dict(accepted=True)


@transaction.atomic
def fail(worker_id,command_id,lease,reason,permanent):
    if reason not in ('area_import_validation_failed','area_import_delivery_failed') or type(permanent) is not bool:raise ValueError('Import failure code')
    c=Command.objects.select_for_update().select_related('actor','normative_set__scope').get(pk=command_id,kind='area.import')
    if c.worker_id!=worker_id or str(c.lease)!=str(lease) or c.state!='delivering' or c.lease_until<=timezone.now():raise Conflict('Import lease')
    require(c.actor,c.normative_set.scope,'review');require(c.actor,c.normative_set.scope,'upload')
    c.state='failed' if permanent or c.attempts>=c.max_attempts else 'pending'
    c.result=dict(reason=reason,attempts=c.attempts);c.lease=None;c.lease_until=None
    c.save(update_fields=['state','result','lease','lease_until'])
    return dict(accepted=True,state=c.state)


@boundary({'POST'},worker=True)
def worker_chunk(request):
    from django.conf import settings
    d=fields(request,{'command_id','lease','sequence','entries','digest'})
    return JsonResponse(receive(settings.KNOWLEDGE_WORKER_ID,d['command_id'],d['lease'],d['sequence'],d['entries'],d['digest']))


def accept(c,result):
    area=c.normative_set
    require(c.actor,area.scope,'review');require(c.actor,area.scope,'upload')
    if area.metadata_revision!=c.payload['area_revision']:raise Conflict('Area changed during import')
    chunks=list(c.analysis_chunks.order_by('sequence'))
    if [x.sequence for x in chunks]!=list(range(len(chunks))):raise Conflict('Import chunk gap')
    entries=[e for chunk in chunks for e in chunk.entries]
    if len(entries)!=result['count'] or digest(entries)!=result['digest']:raise Conflict('Import projection incomplete')
    expected={i['id']:i for i in c.payload['cards']}
    if len(expected)!=len(entries) or {e['id'] for e in entries}!=set(expected):raise Conflict('Import projection selection')
    for p in c.payload['replace_profiles']:
        row=DocumentProfile.objects.get(pk=p['id'],normative_set=area)
        if row.revision!=p['revision']:raise Conflict('Profile changed during import')
    for ref in c.payload['replace_cards']:
        row=ExpertCard.objects.get(pk=ref['id'],source__normative_set=area)
        if row.revision!=ref['revision'] or row.pending_id:raise Conflict('Card changed during import')
    DocumentProfile.objects.filter(pk__in=[p['id'] for p in c.payload['replace_profiles']]).update(archived=True)
    from .profiles import save_profile
    verified_bindings={(b['source_id'],b['profile_id']) for p in c.payload['profiles'] for b in p['definition']['bindings'] if any(i.get('evidence_mode')=='source_fragments' and i['source_id']==b['source_id'] for i in c.payload['cards'])}
    waiting=list(c.payload['profiles']);done=set()
    while waiting:
        ready=[p for p in waiting if set(p['definition']['parents'])<=done]
        if not ready:raise ValueError('Imported profile graph')
        for p in ready:save_profile(c.actor,area.scope_id,p['definition'],p['id'],dataset=area,create_identity=True,verified_bindings=verified_bindings);done.add(p['id']);waiting.remove(p)
    originals={str(x.pk):x for x in area.sources.all()}
    from knowledge_v2.source_identity import FIELDS,VERSION as IDENTITY_VERSION
    for m in c.payload.get('source_metadata',[]):
        source=originals[m['id']]
        if source.identification_revision!=m['revision']:raise Conflict('Source metadata changed during import')
        if not m['fields']:continue
        previous=copy.deepcopy(source.identification)
        source.identification=dict(version=IDENTITY_VERSION,status='imported',source_sha256=source.sha256,confidence=None,manual_fields=list(m['fields']),fields={k:dict(value=m['fields'].get(k,''),citations=[],origin='imported') for k in FIELDS},issues=['Импортированные реквизиты требуют сверки.'])
        source.identification_revision+=1;source.save(update_fields=['identification','identification_revision'])
        audit(c.actor,'source.identity.edited',source.pk,dict(revision=source.identification_revision,reason='Перенос реквизитов из JSON; требуется сверка.',previous=previous,current=source.identification))
    for link in area.trace_links.exclude(payload__status='rejected'):
        p=dict(link.payload,status='rejected',reason='Состав заменён импортом JSON; прежняя связь сохранена в истории.')
        link.revision+=1;p['revision']=link.revision;link.payload=p;link.save()
        NormativeLinkRevision.objects.create(link=link,revision=link.revision,payload=p,digest=digest(p),actor=c.actor,request_key=digest(['area-import-old-link',str(c.pk),str(link.pk)]),intent_digest=digest(p))
    for e in entries:
        i=expected[e['id']]
        if e['source_id']!=i['source_id'] or e['base_id'] not in i['base_ids'] or e['status']!='unreviewed' or e['revision']!=1:raise Conflict('Imported card differs')
        source=originals[e['source_id']];base=ExpertCard.objects.filter(source=source,base_id=e['base_id']).first()
        direct=i.get('evidence_mode')=='source_fragments'
        if not base and not direct:raise Conflict('Import original projection missing')
        p=e['payload']
        if p.get('expert_approved') is not False or p.get('expert_status')!='unreviewed' or p.get('expert_approval') or p.get('validation',{}).get('provenance',{}).get('status')!='verified':raise Conflict('Imported trust was not reset')
        row=ExpertCard.objects.create(pk=e['id'],source=source,analysis=c if direct else base.analysis,base_id=e['base_id'],payload=p,contexts=e.get('contexts',[]) if direct else [],description=p['description'],entity_type=p['entity_type'],profile_key=p.get('profile_id',''),section=p.get('locator',''),status='unreviewed')
        ExpertCardRevision.objects.create(card=row,revision=1,payload=p,digest=digest(p),action='create',reason='Импорт JSON с проверенным первоисточником; требуется экспертная сверка.',actor=c.actor)
    ExpertCard.objects.filter(pk__in=[r['id'] for r in c.payload['replace_cards']]).update(status='superseded')
    from .glossary import sync
    sync(area)
    for choice in c.payload['choices']:
        g=GlossaryGroup.objects.get(normative_set=area,key=choice['key']);g.active_id=choice['active_entry_id'];g.expert_selected=True;g.revision+=1;g.save()
        audit(c.actor,'glossary.activated',g.pk,dict(active=choice['active_entry_id'],reason='Активный вариант из загруженного JSON; экспертная отметка качества не перенесена.'))
    from .trace_links import save
    link_ids={}
    for n,link in enumerate(c.payload['links']):
        data={k:link[k] for k in ('source','target','basis','source_type','target_type','relation','description','condition','mandatory_target','confidence')}
        data.update(status='draft',reason='Импорт междокументного требования из JSON; необходима экспертная сверка. '+link.get('reason',''))
        saved=save(c.actor,area.pk,data,key=digest(['area-import-link',str(c.pk),n]));link_ids[link['id']]=str(saved.pk)
    from .models import ObjectControl
    for control in c.payload.get('controls',[]):
        identity=str(area.pk) if control['kind']=='area' else link_ids.get(control['id'],control['id'])
        ObjectControl.objects.update_or_create(kind=control['kind'],object_id=identity,defaults=dict(normative_set=area,enabled=control['enabled'],deleted=control['deleted']))
    sync(area)
    area.description=c.payload['area']['description'];area.automatic=c.payload['area']['automatic'];area.metadata_revision+=1
    if not area.active_release_id:area.state='prepared'
    area.save()
    audit(c.actor,'area.imported',area.pk,dict(command=str(c.pk),count=len(entries),expert_trust_transferred=False))
    return dict(accepted=True,stats=c.payload['stats'])
