"""Area glossary projection and explicit expert resolution, preserving every source variant."""
from django.db import transaction
from django.db.models import Q,F
from django.http import JsonResponse
from knowledge_v2.glossary import identity,normalized,KINDS
from .models import NormativeSet,ExpertCard,GlossaryGroup,GlossaryVariant,Command
from .access import require,allowed
from .services import audit,Conflict
from .views import boundary,fields


@transaction.atomic
def sync(dataset):
    NormativeSet.objects.select_for_update().get(pk=dataset.pk)
    affected=set(GlossaryGroup.objects.filter(normative_set=dataset).values_list('pk',flat=True))
    cards=ExpertCard.objects.filter(source__normative_set=dataset,entity_type='definition',latest_analysis=True).select_related('source')
    for card in cards:
        try:key,kind,term=identity(card.payload)
        except ValueError:continue  # An unnamed extraction remains a review candidate, never an invented term.
        group,_=GlossaryGroup.objects.get_or_create(normative_set=dataset,key=key)
        old=GlossaryVariant.objects.filter(card=card).first()
        if old:affected.add(old.group_id)
        GlossaryVariant.objects.update_or_create(card=card,defaults=dict(group=group,term=term,kind=kind))
        affected.add(group.pk)
    superseded={s.supersedes_id for s in dataset.sources.all() if s.supersedes_id}
    for group in GlossaryGroup.objects.filter(pk__in=affected):
        variants=list(group.variants.filter(card__latest_analysis=True,card__entity_type='definition').exclude(card__status__in=['rejected','superseded']).exclude(card__source_id__in=superseded).select_related('card__source').order_by('card__source__created','card__source_id','card__section','card_id'))
        from .object_control import selected_cards
        allowed_ids={c.pk for c in selected_cards(dataset,[v.card for v in variants])}
        variants=[v for v in variants if v.card_id in allowed_ids]
        active=next((v.card for v in variants if v.card_id==group.active_id),None) if group.expert_selected else None
        active=active or (variants[0].card if variants else None)
        conflict=len({normalized(v.card.description) for v in variants})>1
        changed=group.active_id!=(active.pk if active else None) or group.conflict!=conflict
        if changed:
            if group.active_id and not any(v.card_id==group.active_id for v in variants):group.expert_selected=False
            group.active=active;group.conflict=conflict;group.revision+=1;group.save()


def metadata(card,detail=False):
    try:v=card.glossary_variant
    except GlossaryVariant.DoesNotExist:return None
    from .object_control import metadata as lifecycle
    data=dict(control=lifecycle('card',card.pk),term=v.term,kind=v.kind,type_label=KINDS[v.kind],active=v.group.active_id==card.pk,
                group_revision=v.group.revision,conflict=v.group.conflict,group_key=v.group.key,
                uploaded=card.source.created.isoformat(),expert_selected=v.group.expert_selected,
                removed=card.status in ('rejected','superseded'))
    if detail:
        data['variants']=[dict(id=str(x.card_id),description=x.card.description,source_name=x.card.source.display_name,section=x.card.section,uploaded=x.card.source.created.isoformat(),active=x.card_id==v.group.active_id) for x in v.group.variants.filter(card__latest_analysis=True).exclude(card__status__in=['rejected','superseded']).select_related('card__source').order_by('card__source__created','card_id')[:30]]
        from .models import AuditEvent
        data['choices']=[dict(reason=e.data.get('reason',''),created=e.created.isoformat(),active=e.data.get('active'),author=e.actor.get_username() if e.actor else 'Система') for e in AuditEvent.objects.filter(action='glossary.activated',aggregate_id=str(v.group_id)).select_related('actor').order_by('-created')[:30]]
    return data


def frozen(dataset,selected_rows):
    groups={}
    for card in selected_rows:
        if card.entity_type!='definition' or card.status in ('rejected','superseded'):continue
        try:key,_,_=identity(card.payload)
        except ValueError:continue
        groups.setdefault(key,[]).append(card)
    result=[]
    choices={g.key:g for g in dataset.glossary_groups.all()}
    for key,variants in sorted(groups.items()):
        g=choices.get(key)
        active=next((c for c in variants if g and c.pk==g.active_id),None)
        active=active or min(variants,key=lambda c:(c.source.created,str(c.source_id),c.section,str(c.pk)))
        result.append(dict(key=key,active=str(active.pk),revision=g.revision if g else 1))
    return result


@boundary({'GET'})
def registry(request,identity):
    dataset=NormativeSet.objects.select_related('scope').get(pk=identity);require(request.user,dataset.scope,'read')
    query=GlossaryVariant.objects.filter(group__normative_set=dataset,card__latest_analysis=True,card__entity_type='definition').select_related('group','card__source')
    superseded=list(dataset.sources.exclude(supersedes=None).values_list('supersedes_id',flat=True))
    query=query.exclude(card__source_id__in=superseded)
    state=request.GET.get('state','all');kind=request.GET.get('kind','');q=request.GET.get('q','')[:300]
    if state not in ('all','active','inactive','removed','conflict') or (kind and kind not in KINDS):raise ValueError('Glossary filter')
    if state!='removed':query=query.exclude(card__status__in=['rejected','superseded'])
    else:query=query.filter(card__status__in=['rejected','superseded'])
    if state=='active':query=query.filter(card_id=F('group__active_id'))
    if state=='inactive':query=query.exclude(card_id=F('group__active_id'))
    if state=='conflict':query=query.filter(group__conflict=True)
    if kind:query=query.filter(kind=kind)
    if q:query=query.filter(Q(term__icontains=q)|Q(card__description__icontains=q)|Q(card__source__filename__icontains=q)|Q(card__source__identification__fields__short_title__value__icontains=q))
    page=int(request.GET.get('page','1'));size=50
    if not 1<=page<=100000:raise ValueError('Page')
    count=query.count();entries=[]
    for v in query.order_by('term','card__source__created','card_id')[(page-1)*size:page*size]:
        c=v.card
        entries.append(dict(id=str(c.pk),revision=c.revision,description=c.description,source_id=str(c.source_id),source_name=c.source.display_name,section=c.section,**metadata(c)))
    return JsonResponse(dict(entries=entries,total=count,page=page,pages=max(1,(count+size-1)//size),can_edit=allowed(request.user,dataset.scope,'review')))


@boundary({'POST'})
def activate(request,identity,card_id):
    dataset=NormativeSet.objects.select_related('scope').get(pk=identity);require(request.user,dataset.scope,'review')
    d=fields(request,{'expected_revision','group_revision','reason'},set())
    if not isinstance(d['reason'],str) or not 5<=len(d['reason'].strip())<=2000:raise ValueError('Reason required')
    with transaction.atomic():
        NormativeSet.objects.select_for_update().get(pk=dataset.pk)
        # Publish one consistent choice; never swap a pending release underneath its manifest.
        if Command.objects.filter(normative_set=dataset,kind__in=['release.prepare','release.publish'],state__in=['pending','delivering']).exists():raise Conflict('Дождитесь завершения публикации нормативной базы.')
        v=GlossaryVariant.objects.select_related('card__source','group').get(card_id=card_id,group__normative_set=dataset)
        c=v.card;g=GlossaryGroup.objects.select_for_update().get(pk=v.group_id)
        from .models import ObjectControl
        control=ObjectControl.objects.filter(kind='card',object_id=c.pk).first()
        if control and control.deleted:raise Conflict('Удалённое определение нельзя активировать.')
        if c.revision!=d['expected_revision'] or g.revision!=d['group_revision']:raise Conflict('Глоссарий изменился. Обновите карточку.')
        if not c.latest_analysis or c.status in ('rejected','superseded') or c.entity_type!='definition' or dataset.sources.filter(supersedes=c.source).exists():raise Conflict('Определение удалено или заменено новой редакцией.')
        if c.pending_id and c.pending.state in ('pending','delivering'):raise Conflict('Дождитесь сохранения правки определения.')
        previous=str(g.active_id) if g.active_id else None
        if control and not control.enabled:
            control.enabled=True;control.revision+=1;control.save()
        g.active=c;g.expert_selected=True;g.revision+=1;g.save()
        audit(request.user,'glossary.activated',g.pk,dict(area=str(dataset.pk),previous=previous,active=str(c.pk),revision=g.revision,reason=d['reason']))
        from .automatic import advance
        advance(dataset,request.user)
    return JsonResponse(dict(active=str(c.pk),group_revision=g.revision,publication='pending' if dataset.automatic else 'manual'))
