"""Business areas are normative sets, never access scopes. All mutations use existing canonical commands."""
import uuid
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse
from .models import NormativeSet, DocumentProfile, ExpertCard, ExperienceReview, Command
from .access import allowed, require
from .views import boundary, fields, set_json
from .services import Conflict, audit, digest
from . import expert

def metrics(dataset):
    from .curation import summary
    cards=ExpertCard.objects.filter(source__normative_set=dataset,latest_analysis=True).exclude(status__in=['rejected','superseded'])
    counts=cards.aggregate(requirements=Count('pk',filter=~Q(entity_type='definition')),terms=Count('pk',filter=Q(entity_type='definition')),confirmed=Count('pk',filter=Q(status='confirmed')))
    info=summary(dataset.active_release) if dataset.active_release_id else {}
    quality=info.get('quality') or {}
    counts.update(ready=quality.get('ready',0),candidates=quality.get('candidate',0),profiles=len(list(area_profiles(dataset))),links=dataset.trace_links.exclude(payload__status='rejected').count())
    counts.update(glossary_active=dataset.glossary_groups.exclude(active=None).count(),glossary_conflicts=dataset.glossary_groups.filter(conflict=True).count())
    return counts


def area_profiles(dataset):
    # Compatibility for old shared profiles: retain bindings until expert explicitly assigns an area.
    ids = [str(x) for x in dataset.sources.values_list('pk', flat=True)]
    for p in DocumentProfile.objects.filter(scope=dataset.scope, archived=False).order_by('name'):
        if p.normative_set_id == dataset.pk or (not p.normative_set_id and any(b.get('source_id') in ids for b in p.definition.get('bindings', []))):
            if not ('глоссар' in p.name.lower() or p.definition.get('kind') == 'glossary'):
                yield p


def profile_json(p):
    from .object_control import metadata
    definition={k:v for k,v in p.definition.items() if k!='bindings'}
    return dict(control=metadata('profile',p.pk),id=str(p.pk), name=p.name, revision=p.revision, definition=definition,
                area_id=str(p.normative_set_id) if p.normative_set_id else None)


@boundary({'GET','POST'})
def search(request,identity):
    dataset=NormativeSet.objects.get(pk=identity);require(request.user,dataset.scope,'read')
    if request.method=='POST':
        from .services import command,NotReady
        d=fields(request,{'query'},{'profile'})
        if not isinstance(d['query'],str) or not 1<=len(d['query'].strip())<=300:raise ValueError('Search query')
        if not dataset.active_release_id:raise NotReady('Area has no published search index')
        payload=dict(set_id=str(dataset.pk),actor_id=request.user.pk,release_id=str(dataset.active_release_id),query=d['query'].strip())
        if d.get('profile'):
            p=DocumentProfile.objects.get(pk=d['profile'],scope=dataset.scope)
            if p not in list(area_profiles(dataset)):raise ValueError('Profile outside area')
            payload['profiles']=[str(p.pk)]
        c=command(request.user,dataset,'normative.search',digest(['area-search',request.user.pk,payload]),payload)
        return JsonResponse({'command_id':str(c.pk),'state':c.state},status=202)
    c=Command.objects.get(pk=request.GET['command'],normative_set=dataset,actor=request.user,kind='normative.search')
    return JsonResponse({'state':c.state,'result':c.result})


@boundary({'GET', 'POST'})
def areas(request):
    from .object_control import metadata
    if request.method == 'POST':
        from .services import create_set
        d = fields(request, {'name', 'scope_id'}, {'description'})
        with transaction.atomic():
            row = create_set(request.user, d['name'], d['scope_id'], request.headers.get('Idempotency-Key'))
            row.automatic = True; row.description = str(d.get('description', ''))[:4000]
            row.save(update_fields=['automatic', 'description'])
        return JsonResponse(set_json(row), status=201)
    return JsonResponse({'areas': [dict(set_json(x), description=x.description, automatic=x.automatic,counts=metrics(x),updated=(x.active_release.created if x.active_release_id else x.created).isoformat(),
        can_edit=allowed(request.user, x.scope, 'review'), can_upload=allowed(request.user, x.scope, 'upload'))
        for x in NormativeSet.objects.select_related('scope','active_release').filter(purpose='normative').order_by('name') if allowed(request.user, x.scope, 'read') and not metadata('area',x.pk)['deleted']]})


@boundary({'GET', 'POST'})
def area(request, identity):
    row = NormativeSet.objects.select_related('scope').get(pk=identity, purpose='normative')
    require(request.user, row.scope, 'read')
    if request.method == 'POST':
        d = fields(request, {'expected_revision'}, {'name', 'description', 'automatic', 'retry'})
        require(request.user, row.scope, 'review')
        with transaction.atomic():
            row = NormativeSet.objects.select_for_update().get(pk=identity)
            if d['expected_revision'] != row.metadata_revision: raise Conflict('Area revision changed')
            if 'name' in d:
                if not isinstance(d['name'], str) or not 1 <= len(d['name'].strip()) <= 160: raise ValueError('Name')
                row.name = d['name'].strip()
            if 'description' in d: row.description = str(d['description'])[:4000]
            if 'automatic' in d:
                if type(d['automatic']) is not bool: raise ValueError('Automatic boolean')
                row.automatic = d['automatic']
            row.metadata_revision += 1; row.save()
            audit(request.user, 'area.updated', row.pk, d)
            from .automatic import advance
            advance(row, request.user, retry=bool(d.get('retry')))
    from .models import AuditEvent
    blocked=AuditEvent.objects.filter(aggregate_id=str(row.pk),action='area.publication_blocked').order_by('-created').first()
    return JsonResponse(dict(set_json(row), description=row.description, automatic=row.automatic,
        profiles=[profile_json(p) for p in area_profiles(row)],glossary_profiles=[profile_json(p) for p in row.profiles.filter(archived=False) if 'глоссар' in p.name.casefold()],counts=metrics(row),publication_warning=blocked.data.get('reason') if blocked and (not row.active_release_id or blocked.created>row.active_release.created) else None,
        can_edit=allowed(request.user, row.scope, 'review'), can_upload=allowed(request.user, row.scope, 'upload'),
        commands=[dict(id=str(c.pk), kind=c.kind, state=c.state, result=c.result) for c in row.command_set.order_by('-created')[:12]]))


@boundary({'GET', 'POST'})
def profiles(request, identity):
    dataset = NormativeSet.objects.select_related('scope').get(pk=identity)
    require(request.user, dataset.scope, 'read')
    if request.method == 'GET': return JsonResponse({'profiles': [profile_json(p) for p in area_profiles(dataset)]})
    d = fields(request, {'definition'}, {'profile_id', 'expected_revision'})
    from .profiles import save_profile
    p = save_profile(request.user, dataset.scope_id, d['definition'], d.get('profile_id'), d.get('expected_revision'), dataset=dataset)
    from .automatic import advance
    advance(dataset, request.user)
    return JsonResponse(profile_json(p), status=201)


@boundary({'POST'})
def remove_profile(request, identity, profile_id):
    dataset = NormativeSet.objects.get(pk=identity); require(request.user, dataset.scope, 'review')
    d = fields(request, {'expected_revision', 'reason'}, {'children_action','children_revisions'})
    with transaction.atomic():
        p = DocumentProfile.objects.select_for_update().get(pk=profile_id, normative_set=dataset)
        if p.revision != d['expected_revision']: raise Conflict('Profile changed')
        if len(str(d['reason']).strip()) < 5: raise ValueError('Reason required')
        dependents = [x for x in area_profiles(dataset) if str(p.pk) in x.definition.get('parents', [])]
        if dependents:
            if d.get('children_action')!='detach':return JsonResponse({'error': 'profile_has_children', 'children': [profile_json(x) for x in dependents]}, status=409)
            if d.get('children_revisions')!={str(x.pk):x.revision for x in dependents}:raise Conflict('Dependent profiles changed')
            from .profiles import save_profile
            for child in dependents:
                definition=dict(child.definition,parents=[x for x in child.definition.get('parents',[]) if x!=str(p.pk)])
                save_profile(request.user,dataset.scope_id,definition,child.pk,child.revision,dataset=dataset)
        from .models import ProfileRevision
        p.archived = True; p.revision += 1; p.save()
        ProfileRevision.objects.create(profile=p, revision=p.revision, definition=p.definition, digest=digest(p.definition), actor=request.user)
        audit(request.user, 'profile.deleted', p.pk, {'reason': d['reason']})
        from .automatic import advance
        advance(dataset, request.user)
    return JsonResponse({'archived': True})


def review_refs(review):
    return review.submission.get('draft', {}).get('normative_refs', [])


def relates(review, dataset):
    return (review.normative_set_id == dataset.pk or review.submission.get('area_id') == str(dataset.pk)
            or any(ref.get('set_id') == str(dataset.pk) for ref in review_refs(review)))


@boundary({'GET','POST'})
def reviews(request, identity):
    dataset = NormativeSet.objects.get(pk=identity); require(request.user, dataset.scope, 'read')
    if request.method == 'POST':
        d=fields(request,{'comment'},{'card_id','profile_id'})
        if not isinstance(d['comment'],str) or not 10<=len(d['comment'].strip())<=12000:raise ValueError('Reasoned review required')
        if not d.get('card_id') and not d.get('profile_id'):raise ValueError('Review target required')
        if d.get('card_id'):ExpertCard.objects.get(pk=d['card_id'],source__normative_set=dataset)
        if d.get('profile_id'):DocumentProfile.objects.get(pk=d['profile_id'],normative_set=dataset,archived=False)
        key=request.headers.get('Idempotency-Key')
        if not key or len(key)>128:raise ValueError('Idempotency key')
        identity=uuid.uuid5(dataset.pk,digest([request.user.pk,key]))
        data=dict(d,area_id=str(dataset.pk))
        row,new=ExperienceReview.objects.get_or_create(pk=identity,defaults=dict(normative_set=dataset,author=request.user,submission=data))
        if not new and (row.author_id!=request.user.pk or row.submission!=data):raise Conflict('Review replay changed')
        return JsonResponse({'id':str(row.pk),'state':row.state},status=201 if new else 200)
    # JSON references in legacy review drafts are also area-owned. Keep both source ACLs.
    rows = ExperienceReview.objects.select_related('author', 'normative_set__scope')
    target = request.GET.get('card') or request.GET.get('profile')
    card=ExpertCard.objects.get(pk=request.GET['card'],source__normative_set=dataset) if request.GET.get('card') else None
    profile=DocumentProfile.objects.get(pk=request.GET['profile'],scope=dataset.scope) if request.GET.get('profile') else None
    if profile and profile not in list(area_profiles(dataset)):raise ValueError('Profile outside area')
    bindings=set(expert.bindings(profile)) if profile else set()
    targets={str(card.pk),str(card.base_id)} if card else set()
    entries = []
    for r in rows.order_by('-created'):
        if not relates(r,dataset) or not allowed(request.user, r.normative_set.scope, 'read'): continue
        if not allowed(request.user,dataset.scope,'review') and r.author_id!=request.user.pk:continue
        direct=target in (r.submission.get('card_id'),r.submission.get('profile_id'),r.submission.get('obligation_id'))
        referenced=any(str(ref.get('requirement_ref',[None])[0]) in targets for ref in review_refs(r))
        matched=Q(payload__local_profile=str(profile.pk)) if profile else Q(pk__in=[])
        for sid,pid in bindings:matched|=Q(source_id=sid,profile_key=pid)
        profile_match=profile and ExpertCard.objects.filter(source__normative_set=dataset,pk=r.submission.get('card_id') or None).filter(matched).exists()
        if target and not (direct or referenced or profile_match): continue
        entries.append(dict(id=str(r.pk), revision=r.revision, state=r.state, author=r.author.get_username(), submission=r.submission, result=r.result))
        if len(entries) == 50: break
    return JsonResponse({'entries': entries})


@boundary({'POST'})
def review_decision(request, identity, review_id):
    dataset = NormativeSet.objects.get(pk=identity); require(request.user, dataset.scope, 'review')
    d = fields(request, {'action', 'expected_revision', 'reason'}, {'card_id', 'patch', 'card_revision','profile_id','profile_revision','definition'})
    with transaction.atomic():
        r = ExperienceReview.objects.select_for_update().get(pk=review_id)
        require(request.user, r.normative_set.scope, 'read')
        if not relates(r,dataset): raise ValueError('Review outside area')
        if r.revision != d['expected_revision'] or r.state in ('rejected', 'used'): raise Conflict('Review changed')
        if len(d['reason'].strip()) < 5 or d['action'] not in ('edit', 'reject'): raise ValueError('Decision')
        result = dict(reason=d['reason'], actor=request.user.pk)
        if d['action'] == 'edit':
            if d.get('profile_id'):
                from .profiles import save_profile
                p=DocumentProfile.objects.get(pk=d['profile_id'],normative_set=dataset,archived=False)
                p=save_profile(request.user,dataset.scope_id,d['definition'],p.pk,d['profile_revision'],dataset=dataset)
                result.update(profile_id=str(p.pk),profile_revision=p.revision);r.state='used'
                from .automatic import advance
                advance(dataset,request.user)
            else:
                card = ExpertCard.objects.get(pk=d['card_id'], source__normative_set=dataset)
                c = expert.submit(request.user, card.pk, dict(action='edit', expected_revision=d['card_revision'], reason=d['reason'], patch=d['patch']), request.headers.get('Idempotency-Key'))
                result.update(command_id=str(c.pk), card_id=str(card.pk)); r.state = 'edit_pending'
        else: r.state = 'rejected'
        r.result = result; r.revision += 1; r.save()
        audit(request.user, 'area.review_decided', r.pk, result)
    return JsonResponse({'state': r.state, 'result': result})
