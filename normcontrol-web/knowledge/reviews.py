"""Explicit knowledge submissions, separate from ordinary finding status changes."""
import uuid
from django.db import transaction
from django.core.exceptions import PermissionDenied
from knowledge_v2.experience import validate_draft, consent_preserved, KINDS
from .access import require, allowed
from .models import NormativeSet, ExperienceReview, Command
from .services import command, digest, audit, Conflict, NotReady


def read(user,rid):
    review=ExperienceReview.objects.select_related('normative_set__scope').get(pk=rid)
    require(user,review.normative_set.scope,'read')
    if review.author_id!=user.pk and not allowed(user,review.normative_set.scope,'publish'):
        raise PermissionDenied('Original review is visible to its author and curator')
    return review


@transaction.atomic
def submit(user,set_id,data,key):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    if dataset.purpose!='experience':raise NotReady('Select an experience set for reviews')
    if dataset.state=='archived':raise NotReady('Restore archived experience set before review')
    require(user,dataset.scope,'review')
    if set(data)!={'task_id','obligation_id','proposal_kind','comment','evidence','draft'}:raise ValueError('Explicit review fields required')
    if data['proposal_kind'] not in KINDS:raise ValueError('Review kind')
    if not isinstance(data['comment'],str) or not 10<=len(data['comment'].strip())<=12000:raise ValueError('Reasoned review required')
    if not isinstance(data['evidence'],list) or not 1<=len(data['evidence'])<=30:raise ValueError('Evidence required')
    draft=validate_draft(data['draft'])
    if draft['scope_id']!=str(dataset.scope_id):raise ValueError('Scope mismatch')
    for ref in draft['normative_refs']:
        origin=NormativeSet.objects.get(pk=ref['set_id']);require(user,origin.scope,'read')
        if origin.purpose!='normative':raise ValueError('Review must reference a normative source')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    identity=digest(['review-submit',user.pk,key]);old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        obj=read(user,old.payload['proposal_id'])
        if obj.normative_set_id!=dataset.pk or obj.submission!=data:raise Conflict('Review retry differs')
        return obj,old
    obj=ExperienceReview.objects.create(normative_set=dataset,author=user,submission=data)
    c=command(user,dataset,'review.submit',identity,dict(data,set_id=str(dataset.pk),proposal_id=str(obj.pk),actor_id=user.pk))
    audit(user,'review.submitted',obj.pk,{'scope_id':str(dataset.scope_id),'proposal_kind':data['proposal_kind']})
    return obj,c


@transaction.atomic
def decide(user,rid,action,data,key):
    obj=ExperienceReview.objects.select_for_update().select_related('normative_set__scope').get(pk=rid)
    require(user,obj.normative_set.scope,'review' if action=='suggest' else 'publish')
    if action=='suggest':read(user,rid)
    if action not in ('approve','reject','revoke','repair','suggest'):raise ValueError('Decision action')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if set(data)-{'expected_revision','draft','reason','experience_id','version','normative_ref','corrected_card'} or 'expected_revision' not in data:raise ValueError('Decision fields')
    request_key=digest(['review-decision',user.pk,key])
    body=dict(data,set_id=str(obj.normative_set_id),proposal_id=str(obj.pk),actor_id=user.pk,request_digest=digest(data))
    old=Command.objects.filter(idempotency_key=request_key).first()
    if old:
        if old.payload.get('request_digest')!=digest(data) or old.kind!='review.'+action or old.payload['proposal_id']!=str(obj.pk):raise Conflict('Decision retry differs')
        return obj,old
    if data['expected_revision']!=obj.revision:raise Conflict('Review revision changed')
    if Command.objects.filter(normative_set=obj.normative_set,payload__proposal_id=str(obj.pk),state__in=['pending','delivering']).exists():raise NotReady('Previous review command is still pending')
    if action=='approve':
        if obj.state not in ('pending','approved','revoked'):raise NotReady('Review is not ready for curation')
        draft=validate_draft(data['draft'])
        if obj.submission['proposal_kind'] in ('extraction_error','norm_change','unsubstantiated'):raise NotReady('Requires corrected normative record/source or new justified review')
        if draft['scope_id']!=str(obj.normative_set.scope_id) or not draft['sharing_confirmed'] or not obj.submission['draft']['sharing_confirmed']:raise PermissionDenied('Explicit scope and sharing consent required')
        if not consent_preserved(obj.submission['draft'],draft):raise PermissionDenied('Curator cannot broaden author applicability')
        for ref in draft['normative_refs']:require(user,NormativeSet.objects.get(pk=ref['set_id']).scope,'read')
    elif action=='suggest':
        if obj.state!='pending':raise NotReady('Only pending reviews need a draft suggestion')
    elif action=='repair':
        if obj.state!='pending' or obj.submission['proposal_kind']!='extraction_error':raise NotReady('A pending extraction-error review is required')
        target=data['normative_ref']
        if target not in obj.submission['draft']['normative_refs']:raise ValueError('Correction outside reviewed sources')
        require(user,NormativeSet.objects.get(pk=target['set_id']).scope,'publish')
        if not isinstance(data.get('corrected_card'),dict):raise ValueError('Corrected card required')
    elif not isinstance(data.get('reason'),str) or not data['reason'].strip():raise ValueError('Decision reason required')
    if action=='reject' and obj.state!='pending':raise NotReady('Only a pending proposal can be rejected')
    if action=='revoke':
        if obj.state!='approved':raise NotReady('Only approved experience can be revoked')
        body.update(experience_id=obj.result['record_id'],version=obj.result['version'])
    elif action=='approve' and obj.state in ('approved','revoked'):
        body.update(experience_id=obj.result['record_id'],version=obj.result['version']+1)
    c=command(user,obj.normative_set,'review.'+action,request_key,body)
    obj.revision+=1;obj.state='decision_pending';obj.save(update_fields=['revision','state'])
    audit(user,'review.'+action+'_requested',obj.pk,{'revision':obj.revision,'reason':data.get('reason','Explicit curation')})
    return obj,c


@transaction.atomic
def publish(user,set_id,expected_revision,key):
    dataset=NormativeSet.objects.select_for_update().get(pk=set_id)
    if dataset.purpose!='experience':raise NotReady('Only experience sets publish lessons')
    if dataset.state=='archived':raise NotReady('Restore the set before publication')
    require(user,dataset.scope,'publish')
    if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Idempotency-Key required')
    if type(expected_revision) is not int:raise ValueError('Metadata revision')
    payload=dict(set_id=str(dataset.pk),actor_id=user.pk,expected_revision=expected_revision)
    identity=digest(['experience-publish',user.pk,key]);old=Command.objects.filter(idempotency_key=identity).first()
    if old:
        if old.payload!=payload:raise Conflict('Publication retry differs')
        return old
    if dataset.metadata_revision!=expected_revision:raise Conflict('Set revision changed')
    if Command.objects.filter(normative_set=dataset,kind__in=['experience.publish','release.publish'],state__in=['pending','delivering']).exists():raise Conflict('Publication already pending')
    c=command(user,dataset,'experience.publish',identity,payload)
    audit(user,'experience.publish_requested',dataset.pk)
    return c
