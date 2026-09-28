"""Idempotent publication state machine. A failed stage retains the last successful release."""
import re
from django.db import transaction
from .models import Command, SourceUpload, ExpertCard, ExperienceReview
from .services import digest, NotReady, Conflict


@transaction.atomic
def advance(dataset, actor, retry=False):
    from .object_control import metadata,excluded,selected_cards
    if not dataset.automatic or dataset.state == 'archived' or not metadata('area',dataset.pk)['enabled']: return
    from .models import NormativeSet
    dataset = NormativeSet.objects.select_for_update().get(pk=dataset.pk)
    if retry:
        failed=Command.objects.filter(normative_set=dataset,state='failed').exclude(kind__in=['review.execute','normative.search']).order_by('created').first()
        if failed:
            failed.state='pending';failed.attempts=0;failed.lease=None;failed.lease_until=None;failed.save();return
    active = Command.objects.filter(normative_set=dataset, state__in=['pending', 'delivering']).exclude(kind__in=['review.execute','normative.search'])
    if active.exists(): return
    sources = list(dataset.sources.exclude(state='error').order_by('created'))
    # Only the last revision in each source family enters a release.
    superseded = {s.supersedes_id for s in sources if s.supersedes_id}
    sources = [s for s in sources if s.pk not in superseded and str(s.pk) not in excluded(dataset,'source')]
    if not sources or any(s.state not in ('prepared', 'partial') for s in sources): return
    from .services import analyze_source, prepare_selected_sources, publish
    for source in sources:
        last = Command.objects.filter(normative_set=dataset, kind='source.analyze', payload__source_id=str(source.pk)).order_by('-created').first()
        if not last:
            analyze_source(actor, dataset.pk, source.pk, digest(['automatic-analysis',str(source.pk)]))
            return
        if last.state != 'done': return
    cards = list(ExpertCard.objects.filter(source__normative_set=dataset, latest_analysis=True).exclude(status__in=['rejected','superseded']).order_by('id'))
    cards=selected_cards(dataset,cards)
    fingerprint = digest([dataset.metadata_revision,[(str(c.pk), c.revision) for c in cards],[(str(p),r) for p,r in dataset.profiles.filter(archived=False).values_list('id','revision')],list(dataset.glossary_groups.order_by('key').values_list('key','active_id','revision'))])
    # Cover pairs of bounded blocks across ALL area sources, not only adjacent cards from one file.
    from .trace_links import suggest
    groups = [cards[n:n+6] for n in range(0,len(cards),6)]
    from collections import defaultdict, Counter
    inverted=defaultdict(list); terms=[]
    for n,g in enumerate(groups):
        ts={t for c in g for t in re.findall(r'[\w]{4,}',c.description.casefold())}
        terms.append(ts)
        for t in ts: inverted[t].append(n)
    pairs=set()
    for n,ts in enumerate(terms):
        scores=Counter(k for t in ts for k in inverted[t] if k!=n and len(inverted[t])<=max(20,len(groups)//5))
        for k,_ in scores.most_common(3):pairs.add(tuple(sorted((n,k))))
    combinations = [(g, []) for g in groups] + [(groups[a],groups[b]) for a,b in sorted(pairs)]
    for left, right in combinations:
        ids = [str(c.pk) for c in left+right]
        if len(ids)<2: continue
        key=digest(['auto-area-links',fingerprint,ids])
        c = Command.objects.filter(idempotency_key=digest(['trace-suggest',actor.pk,key])).first()
        if not c: suggest(actor,dataset.pk,ids,key); return
        if c.state!='done': return
    key=digest(['auto-area-release', fingerprint,[(str(l),r) for l,r in dataset.trace_links.values_list('id','revision')]])
    existing = Command.objects.filter(idempotency_key=digest(['prepare-selected',actor.pk,key])).first()
    if existing and existing.state == 'done':
        release=dataset.releases.get(pk=existing.payload['release_id'])
        if release.state == 'ready':
            publish(actor,dataset.pk,release.pk,dataset.metadata_revision,key)
        return
    if existing: return
    try:
        summaries=[Command.objects.filter(normative_set=dataset,kind='source.analyze',payload__source_id=str(s.pk)).order_by('-created').first().result['summary'] for s in sources]
        mode='screened_test' if any(s.get('semantic_completeness')=='partial' for s in summaries) else 'complete'
        prepare_selected_sources(actor,dataset.pk,[str(s.pk) for s in sources],dataset.metadata_revision,key,mode=mode)
    except (NotReady, Conflict) as e:
        from .services import audit
        audit(actor,'area.publication_blocked',dataset.pk,{'reason':str(e)})


def completed(command):
    dataset=command.normative_set
    if command.kind=='expert.apply':
        for review in ExperienceReview.objects.filter(state='edit_pending',result__command_id=str(command.pk)):
            review.state='used';review.result=dict(review.result,card_versions=command.result.get('card_ids',[]));review.save(update_fields=['state','result'])
    if command.kind in ('source.ingest','source.analyze','expert.apply','area.import','trace.suggest','release.prepare'):
        advance(dataset,command.actor)
