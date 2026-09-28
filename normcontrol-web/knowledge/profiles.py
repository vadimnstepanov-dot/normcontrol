"""Administrator-owned, versioned declarative profiles. No executable user expressions."""
import uuid
from django.core.exceptions import PermissionDenied
from django.db import transaction
from knowledge_v2.applicability import validate_expression
from .access import require
from .models import Scope, DocumentProfile, ProfileRevision, SourceUpload, Command
from .services import Conflict, digest, audit


def activate_extracted_profiles(command,source,profiles):
    """Worker projection only, within event transaction/lease/ACL validation. Never edits administrator revisions."""
    from knowledge_v2.applicability import validate_profile
    scope=source.normative_set.scope
    index={p['id']:validate_profile(p) for p in profiles}
    if len(index)!=len(profiles):raise ValueError('Duplicate extracted profile')
    ordered=[];visited=set()
    def visit(identity,path):
        if identity in path or len(path)>12:raise ValueError('Invalid extracted profile graph')
        if identity in visited:return
        if identity not in index:raise ValueError('Missing extracted parent')
        for parent in index[identity].get('inherits',[]):visit(parent,path|{identity})
        visited.add(identity);ordered.append(index[identity])
    for identity in index:visit(identity,set())
    for p in ordered:
        glossary=p.get('key')=='glossary'
        identity=str(uuid.uuid5(uuid.UUID(str(scope.pk)),'shared-normative-glossary')) if glossary else p['id']
        definition=dict(name=p['name'],expression=p['expression'],parents=p.get('inherits',[]),
            bindings=[dict(source_id=str(source.pk),profile_id=p['id'])],
            description='Автоматически выделен из нормативного источника. Требования требуют публикации и отдельного экспертного решения.')
        row,created=DocumentProfile.objects.get_or_create(id=identity,defaults=dict(scope=scope,name=p['name'],definition=definition))
        if row.scope_id!=scope.pk:raise Conflict('Extracted profile ownership conflict')
        if created:
            ProfileRevision.objects.create(profile=row,revision=1,definition=definition,digest=digest(definition),actor=command.actor)
            audit(command.actor,'profile.auto_created',row.pk,{'command_id':str(command.pk),'expert_approved':False})
        elif glossary:
            binding=definition['bindings'][0]
            if binding not in row.definition['bindings']:
                updated=dict(row.definition,bindings=[*row.definition['bindings'],binding])
                # Extend only the generated source selector; preserve an administrator's custom predicate.
                old_sources=sorted({b['source_id'] for b in row.definition['bindings']})
                predicate=row.definition.get('expression',{}).get('fact',{})
                if predicate.get('name')=='selected_sources' and sorted(predicate.get('in',[]))==old_sources:
                    updated['expression']={'fact':{'name':'selected_sources','in':sorted({b['source_id'] for b in updated['bindings']})}}
                row.definition=updated;row.revision+=1;row.save(update_fields=['definition','revision','updated'])
                ProfileRevision.objects.create(profile=row,revision=row.revision,definition=updated,digest=digest(updated),actor=command.actor)
                audit(command.actor,'profile.glossary_source_added',row.pk,{'command_id':str(command.pk),'revision':row.revision})


@transaction.atomic
def save_profile(user,scope_id,definition,profile_id=None,expected_revision=None):
    if not user.is_authenticated or not user.is_active or not user.is_staff:raise PermissionDenied('Administrator required')
    scope=Scope.objects.select_for_update().get(pk=scope_id);require(user,scope,'manage')
    keys={'name','expression','parents','bindings','description'}
    if not isinstance(definition,dict) or set(definition)!=keys:raise ValueError('Profile fields')
    if not isinstance(definition['name'],str) or not 1<=len(definition['name'].strip())<=160:raise ValueError('Profile name')
    if not isinstance(definition['description'],str) or not definition['description'].strip() or len(definition['description'])>4000:raise ValueError('Profile explanation required')
    validate_expression(definition['expression'])
    if not isinstance(definition['parents'],list) or len(definition['parents'])>30 or len(set(definition['parents']))!=len(definition['parents']):raise ValueError('Parent profiles')
    if not isinstance(definition['bindings'],list) or len(definition['bindings'])>200:raise ValueError('Source profile bindings')
    identity=str(uuid.UUID(str(profile_id))) if profile_id else str(uuid.uuid4())
    index={str(p.pk):p.definition for p in DocumentProfile.objects.filter(scope=scope)}
    for parent in definition['parents']:
        if parent not in index:raise ValueError('Parent outside selected scope')
    index[identity]=definition
    visited=set()
    def visit(pid,path):
        if pid in path or len(path)>12:raise ValueError('Cyclic or too deep profile inheritance')
        if pid in visited:return
        for parent in index[pid]['parents']:visit(parent,path|{pid})
        visited.add(pid)
    visit(identity,set())
    for binding in definition['bindings']:
        if not isinstance(binding,dict) or set(binding)!={'source_id','profile_id'}:raise ValueError('Binding fields')
        source=SourceUpload.objects.get(pk=binding['source_id'],normative_set__scope=scope)
        analyses=Command.objects.filter(normative_set=source.normative_set,kind='source.analyze',state='done',payload__source_id=str(source.pk))
        if not any(binding['profile_id'] in [p['id'] for p in c.result.get('summary',{}).get('profiles',[])] for c in analyses):
            raise ValueError('Source profile not analyzed')
    if profile_id:
        row=DocumentProfile.objects.select_for_update().get(pk=identity,scope=scope)
        if type(expected_revision) is not int or row.revision!=expected_revision:raise Conflict('Profile changed; reload before saving')
        row.revision+=1;row.name=definition['name'].strip();row.definition=definition;row.save()
    else:
        row=DocumentProfile.objects.create(id=identity,scope=scope,name=definition['name'].strip(),definition=definition)
    ProfileRevision.objects.create(profile=row,revision=row.revision,definition=definition,digest=digest(definition),actor=user)
    audit(user,'profile.saved',row.pk,{'revision':row.revision,'digest':digest(definition)})
    return row


def snapshot_profiles(user,ids):
    """Freeze the complete ancestor graph for a later job; editing cannot mutate this value."""
    result={};visiting=set()
    def add(identity):
        identity=str(identity)
        if identity in visiting:raise ValueError('Profile cycle')
        if identity in result:return
        p=DocumentProfile.objects.get(pk=identity);require(user,p.scope,'read');visiting.add(identity)
        for parent in p.definition['parents']:add(parent)
        visiting.remove(identity)
        result[identity]={'id':identity,'revision':p.revision,'definition':p.definition,'digest':digest(p.definition)}
    for identity in ids:add(identity)
    return list(result.values())
