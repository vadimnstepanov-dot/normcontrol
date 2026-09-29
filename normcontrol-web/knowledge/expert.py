"""Paged expert read model and durable user intentions; no model work on VPS."""
import copy
import uuid
from django.db import transaction
from django.db.models import Q
from .models import ExpertCard,ExpertCardRevision,SourceUpload,Command,DocumentProfile
from .access import require,allowed
from .services import Conflict,NotReady,digest,audit,command
from knowledge_v2.expert import edited


def project(command,source,entries):
    """Called only after verified worker journal delivery, or explicit backfill of it."""
    protected=list(ExpertCard.objects.filter(source=source).filter(Q(revision__gt=1)|Q(history__action='create')).distinct())
    protected_citations={digest(c.payload.get('citations',[])) for c in protected}
    ExpertCard.objects.filter(source=source,revision=1).exclude(analysis=command).update(latest_analysis=False)
    # Include expert-created split/merge children, not just extraction entries.
    ExpertCard.objects.filter(source=source,analysis=command,latest_analysis=False).update(latest_analysis=True)
    for payload in entries:
        blocked=digest(payload.get('citations',[])) in protected_citations
        identity=uuid.uuid5(command.pk,str(payload['id']))
        description=payload.get('description') or '; '.join(x.get('description') or x.get('object','') for x in payload.get('obligations',[]))
        card,created=ExpertCard.objects.get_or_create(pk=identity,defaults=dict(source=source,analysis=command,
            base_id=payload['id'],payload=payload,description=description,
            entity_type=payload.get('entity_type','requirement'),profile_key=(payload.get('profile_id') or ''),
            section=str(payload.get('locator',''))))
        if created:ExpertCardRevision.objects.create(card=card,revision=1,payload=payload,digest=digest(payload),action='extracted',actor=None)
        if created and blocked:
            card.status='superseded';card.save(update_fields=['status'])
    if protected:ExpertCard.objects.filter(pk__in=[c.pk for c in protected]).update(latest_analysis=True)
    from .glossary import sync
    sync(source.normative_set)


def bindings(profile):
    seen=set();pairs=set()
    def visit(p):
        if p.pk in seen:return
        seen.add(p.pk)
        pairs.update((x['source_id'],x['profile_id']) for x in p.definition.get('bindings',[]))
        for parent in p.definition.get('parents',[]):
            visit(DocumentProfile.objects.get(pk=parent,scope=p.scope))
    visit(profile)
    return pairs


def inherited(card,profile):
    if not profile:return False
    if card.payload.get('local_profile'):return card.payload['local_profile']!=str(profile.pk)
    local={(b['source_id'],b['profile_id']) for b in profile.definition.get('bindings',[])}
    return (str(card.source_id),card.profile_key) not in local


def card_json(card,detail=False,profile=None,user=None):
    from .object_control import metadata
    data=dict(control=metadata('card',card.pk),id=str(card.pk),revision=card.revision,description=card.description,entity_type=card.entity_type,
        status=card.status,section=card.section,profile_key=card.profile_key,source_id=str(card.source_id),
        source_name=card.source.display_name,set_id=str(card.source.normative_set_id),
        inherited=inherited(card,profile),pending=str(card.pending_id) if card.pending_id and card.pending.state in ('pending','delivering') else None)
    if detail:
        owners=[]
        for p in DocumentProfile.objects.filter(scope=card.source.normative_set.scope):
            if str(p.pk)==card.payload.get('local_profile') or (str(card.source_id),card.profile_key) in {(b['source_id'],b['profile_id']) for b in p.definition.get('bindings',[])}:
                owners.append(dict(id=str(p.pk),name=p.name))
        from .curation import current_context,release_command
        from knowledge_v2.curation import trust
        release=card.source.normative_set.active_release
        published=False
        if release:
            cmd=release_command(release)
            published=bool(cmd and any(x['id']==str(card.pk) and x['revision']==card.revision for x in cmd.payload.get('curation',{}).get('cards',[])))
            if published:
                from .curation import release_catalog
                from knowledge_v2.quality import LABELS
                entry=next((x for x in release_catalog(release) if x['lineage']==str(card.pk) and x['revision']==card.revision),{})
                q=entry.get('quality')
                if q:data.update(quality=q,quality_reasons=[LABELS.get(x,x) for x in q['reasons']])
        data.update(payload=card.payload,contexts=card.contexts,owners=owners,
            can_edit=allowed(user,card.source.normative_set.scope,'review') and not data['inherited'] and card.latest_analysis,
            can_refine=allowed(user,card.source.normative_set.scope,'review') and data['inherited'] and card.latest_analysis and card.status not in ('rejected','superseded'),
            trust=trust(card.payload,card.payload.get('expert_approval'),current_context(user,card)),
            publication='published' if published else 'draft',analysis_state=card.analysis.state,history_count=card.history.count(),
            analysis_partial=card.analysis.result.get('summary',{}).get('semantic_completeness')!='model_reviewed',
        history=[dict(revision=h.revision,action=h.action,reason=h.reason,created=h.created.isoformat(),
                author=h.actor.get_username() if h.actor else 'Модель',digest=h.digest)
                for h in card.history.select_related('actor').order_by('-revision')[:30]])
    if card.entity_type=='definition':
        from .glossary import metadata
        data['glossary']=metadata(card,detail)
    return data


@transaction.atomic
def submit(user,identity,data,key):
    first=ExpertCard.objects.select_related('source__normative_set__scope').get(pk=identity)
    dataset=first.source.normative_set
    # Serialize intentions against source projection updates and split/merge.
    from .models import NormativeSet
    NormativeSet.objects.select_for_update().get(pk=dataset.pk)
    action=data['action'];require(user,dataset.scope,'read' if action=='inspect' else 'review')
    if action not in ('inspect','edit','confirm','reject','split','merge','refine','create'):raise ValueError('Action')
    request_id=digest(['expert',user.pk,key])
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency key')
    old=Command.objects.filter(idempotency_key=request_id).first()
    intent=digest([str(identity),data])
    if old:
        if old.payload.get('intent_digest')!=intent:raise Conflict('Retry differs')
        return old
    if action!='inspect' and Command.objects.filter(normative_set=dataset,
            kind__in=['release.prepare','release.publish'],state__in=['pending','delivering']).exists():
        raise Conflict('Дождитесь завершения подготовки или публикации нормативного выпуска.')
    selected=data.get('others',[]) if action=='merge' else []
    if not isinstance(selected,list) or len(selected)>9:raise ValueError('Selection')
    refs=[dict(id=str(identity),revision=data['expected_revision']),*selected]
    if len({x['id'] for x in refs})!=len(refs):raise ValueError('Duplicate card')
    cards=[]
    for r in refs:
        row=ExpertCard.objects.select_for_update().select_related('source__normative_set__scope').get(pk=r['id'])
        if row.source_id!=first.source_id:raise ValueError('Different source')
        if type(r['revision']) is not int or row.revision!=r['revision']:raise Conflict('Reload changed version')
        if row.pending_id:
            pending=row.pending
            if pending.state in ('pending','delivering'):raise Conflict('Previous decision pending')
            row.pending=None;row.save(update_fields=['pending'])
        if action!='inspect' and not row.latest_analysis:raise Conflict('A newer analysis exists')
        cards.append(row)
    if data.get('profile_id'):
        p=DocumentProfile.objects.get(pk=data['profile_id'],scope=dataset.scope)
        if action not in ('inspect','refine','create') and any(inherited(x,p) for x in cards):raise Conflict('Edit inherited requirement at owner')
        if action=='refine':
            if not inherited(first,p):raise ValueError('Local refinement requires an inherited base')
            if first.payload.get('local_profile'):
                from knowledge_v2.curation import profile_graph
                ps=[dict(id=str(x.pk),definition=x.definition) for x in DocumentProfile.objects.filter(scope=dataset.scope)]
                if first.payload['local_profile'] not in profile_graph(ps)[1][str(p.pk)]:raise ValueError('Base does not belong to selected hierarchy')
            elif (str(first.source_id),first.profile_key) not in bindings(p):raise ValueError('Base does not belong to selected hierarchy')
    elif action=='refine':raise ValueError('Choose a child profile')
    if action!='inspect' and not 5<=len(data.get('reason','').strip())<=2000:raise ValueError('Explain decision')
    if action=='edit':edited(first.payload,data['patch'])
    payload=dict(set_id=str(dataset.pk),source_id=str(first.source_id),actor_id=user.pk,action=action,
        cards=[dict(id=str(x.pk),base_id=str(x.base_id),revision=x.revision) for x in cards],intent_digest=intent)
    for name in ('reason','patch','parts','description','acknowledge_questions','split_logic','resolution_reason','review_import_completeness',
                 'refinement_kind','profile_id','condition','basis_index'):
        if name in data:payload[name]=data[name]
    if action=='confirm':
        from .curation import current_context
        payload['context_digest']=current_context(user,first)
    c=command(user,dataset,'expert.apply',request_id,payload)
    for row in cards:row.pending=c;row.save(update_fields=['pending'])
    audit(user,'expert.requested',first.pk,{'command':str(c.pk),'action':action})
    return c


def accept(command,payload):
    """Inside the authenticated, atomic event boundary."""
    refs={x['id']:x for x in command.payload['cards']}
    original=ExpertCard.objects.select_related('source').get(pk=next(iter(refs)))
    for update in payload['updates']:
        if command.payload['action']=='inspect':
            card=ExpertCard.objects.get(pk=update['id'],pending=command)
            if card.revision!=update['revision']:raise Conflict('Stale context')
            card.contexts=update['contexts'];card.pending=None;card.save(update_fields=['contexts','pending']);continue
        card=ExpertCard.objects.filter(pk=update['id']).first()
        if card:
            if str(card.pk) not in refs or card.pending_id!=command.pk or update['revision']!=card.revision+1:raise Conflict('Stale expert response')
        else:
            if command.payload['action'] not in ('split','merge','refine','create') or update['revision']!=1:raise Conflict('Unexpected new card')
            card=ExpertCard(id=update['id'],source=original.source,analysis=original.analysis,base_id=update['base_id'])
        p=update['payload'];card.payload=p;card.revision=update['revision'];card.status=update['status'];card.pending=None
        card.description=p.get('description','');card.entity_type=p.get('entity_type','requirement');card.profile_key=(p.get('profile_id') or '');card.section=p.get('locator','')
        card.save()
        ExpertCardRevision.objects.create(card=card,revision=card.revision,payload=p,digest=digest(p),
            action=update['action'],reason=update['reason'],actor=command.actor)
    audit(command.actor,'expert.applied',original.pk,{'action':command.payload['action'],'cards':len(payload['updates'])})
    from .glossary import sync
    sync(original.source.normative_set)
    return dict(accepted=True,card_ids=[x['id'] for x in payload['updates']])
