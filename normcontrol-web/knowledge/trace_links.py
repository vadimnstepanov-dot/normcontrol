"""Editable, versioned configuration; release preparation pins and validates it locally."""
import uuid
from django.db import transaction
from .models import NormativeLink,NormativeLinkRevision,NormativeSet,ExpertCard,Command,DocumentProfile
from .access import require
from .services import Conflict,digest,audit,command
from .curation import current_context
from knowledge_v2.trace import validate_link


def pins(user,cards):
    return {k:dict(revision=c.revision,digest=digest(c.payload),context=current_context(user,c)) for k,c in cards.items()}


@transaction.atomic
def save(user,set_id,data,identity=None,key=None):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id);require(user,dataset.scope,'review' if identity else 'upload')
    if dataset.purpose!='normative' or dataset.state=='archived':raise ValueError('Normative set required')
    key=key or uuid.uuid4().hex
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency key')
    request_key=digest(['trace-edit',user.pk,key]);intent=digest([str(set_id),str(identity) if identity else None,data])
    previous=NormativeLinkRevision.objects.select_related('link').filter(request_key=request_key).first()
    if previous:
        if previous.intent_digest!=intent:raise Conflict('Trace retry changed')
        row=previous.link;row.payload=previous.payload;row.revision=previous.revision;return row
    row=NormativeLink.objects.select_for_update().get(pk=identity,normative_set=dataset) if identity else None
    if row and data.get('expected_revision')!=row.revision:raise Conflict('Trace revision changed')
    if data.get('status')=='confirmed':require(user,dataset.scope,'review')
    keys=('source','target','basis')
    cards={k:ExpertCard.objects.get(pk=data[k],source__normative_set=dataset,latest_analysis=True) for k in keys}
    if any(c.pending_id and c.pending.state in ('pending','delivering') for c in cards.values()):raise Conflict('Card edit pending')
    value={k:data[k] for k in ('source','target','basis','source_type','target_type','relation','description','condition',
                                  'mandatory_target','confidence','status','reason')}
    if len(value['reason'].strip())<20:raise ValueError('Explain normative relation in at least 20 characters')
    value.update(id=str(row.pk) if row else str(uuid.uuid4()),revision=row.revision+1 if row else 1,pins=pins(user,cards))
    validate_link(value)
    if row:row.revision=value['revision'];row.payload=value;row.save(update_fields=['revision','payload','updated'])
    else:row=NormativeLink.objects.create(id=value['id'],normative_set=dataset,payload=value)
    NormativeLinkRevision.objects.create(link=row,revision=row.revision,payload=value,digest=digest(value),actor=user,
        request_key=request_key,intent_digest=intent)
    audit(user,'trace_link.saved',row.pk,{'revision':row.revision,'status':value['status']});return row


def frozen(dataset,source_ids):
    result=[];selected=set(map(str,source_ids))
    from .object_control import excluded,selected_cards
    disabled=excluded(dataset,'link')
    for row in dataset.trace_links.order_by('id'):
        if str(row.pk) in disabled:continue
        if row.payload.get('status')=='rejected':continue
        card_ids=[row.payload[k] for k in ('source','target','basis')]
        cards=list(ExpertCard.objects.filter(pk__in=card_ids))
        if len(selected_cards(dataset,cards))!=len(cards):continue
        if row.revision==1 and row.payload.get('status')=='draft' and row.payload.get('reason','').startswith('Автоматически выявлено LLM'):
            by_id={str(c.pk):c for c in cards}
            if any(by_id.get(row.payload[k]) is None or by_id[row.payload[k]].revision!=row.payload.get('pins',{}).get(k,{}).get('revision') or by_id[row.payload[k]].status in ('rejected','superseded') for k in ('source','target','basis')):
                continue  # Obsolete automatic candidates remain in history, never block a new release.
        # Partly selected links cannot silently disappear from publication.
        if any(str(c.source_id) in selected for c in cards):
            if any(str(c.source_id) not in selected for c in cards):raise Conflict('Include all normative sources of trace links')
            result.append(row.payload)
    return result


def suggest(user,set_id,ids,key):
    dataset=NormativeSet.objects.get(pk=set_id);require(user,dataset.scope,'upload')
    if not isinstance(ids,list) or not 2<=len(ids)<=12 or len(set(ids))!=len(ids):raise ValueError('Select 2 to 12 cards')
    cards=list(ExpertCard.objects.filter(pk__in=ids,source__normative_set=dataset,latest_analysis=True))
    if len(cards)!=len(ids):raise ValueError('Missing selected cards')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency key')
    payload=dict(set_id=str(dataset.pk),actor_id=user.pk,cards=[dict(id=str(c.pk),revision=c.revision,
        digest=digest(c.payload),base_id=str(c.base_id),payload=c.payload) for c in sorted(cards,key=lambda c:str(c.pk))])
    from knowledge_v2.document_types import declared_types
    profiles=DocumentProfile.objects.filter(scope=dataset.scope)
    payload['document_types']=declared_types({str(p.pk):dict(kind='profile',payload={'definition':p.definition}) for p in profiles})
    identity=digest(['trace-suggest',user.pk,key]);old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        if old.payload!=payload:raise Conflict('Suggestion retry changed')
        return old
    return command(user,dataset,'trace.suggest',identity,payload)
