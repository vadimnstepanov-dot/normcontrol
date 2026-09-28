"""Pure versioned curation contracts shared by portal and canonical worker."""
import copy
from collections import defaultdict
from .store import checksum

VERSION='curation-9.1.5'
SEMANTIC_FIELDS=('description','entity_type','modality','obligations','conditions','exceptions',
    'applicability','dependencies','composition','composition_group','term','glossary_kind','refinement','applicability_note')
ADDRESS_KEYS={'id','locator','start','end','quote','context_hash','source_sha256','source_id','citation','citations'}


def semantic_hash(card):
    def meaning(value):
        if isinstance(value,dict):return {k:meaning(v) for k,v in value.items() if k not in ADDRESS_KEYS}
        if isinstance(value,list):return [meaning(v) for v in value]
        return value
    return checksum(meaning({k:card.get(k) for k in SEMANTIC_FIELDS}))


def fingerprint(identity,source_id,profile_key,card,status='unreviewed'):
    return dict(id=str(identity),source_id=str(source_id),profile_key=profile_key or '',
        local_profile=card.get('local_profile'),semantic=semantic_hash(card),
        evidence=checksum(card.get('citations',[])),active=status not in ('rejected','superseded'))


def profile_graph(profiles):
    index={p['id']:p for p in profiles}
    if len(index)!=len(profiles):raise ValueError('Duplicate profile identity')
    closure={}
    def visit(pid,trail):
        if pid in trail or len(trail)>12:raise ValueError('Profile cycle/depth')
        if pid not in index:raise ValueError('Missing parent profile')
        if pid not in closure:
            found={pid}
            for parent in index[pid]['definition'].get('parents',[]):found.update(visit(parent,trail|{pid}))
            closure[pid]=found
        return closure[pid]
    for pid in index:visit(pid,set())
    return index,closure


def owners(item,profiles):
    if item.get('local_profile'):
        return [p['id'] for p in profiles if p['id']==item['local_profile']]
    return sorted(p['id'] for p in profiles if any(str(b['source_id'])==item['source_id']
        and (b['profile_id'] or '')==item['profile_key'] for b in p['definition'].get('bindings',[])))


def context_digest(item,profiles,items):
    index,closure=profile_graph(profiles)
    relevant=set().union(*(closure[p] for p in owners(item,profiles)))
    inherited=[]
    for other in items:
        if other['id']!=item['id'] and set(owners(other,profiles))&relevant:
            inherited.append(other)
    return checksum(dict(self=item,profiles=[index[p] for p in sorted(relevant)],
        related=sorted(inherited,key=lambda x:x['id'])))


def trust(card,approval,current_context,provenance_ok=True):
    """Expert mark does not override missing origin, scope or critical context."""
    blocks=[];validation=card.get('validation',{})
    if not provenance_ok or validation.get('provenance',{}).get('status')!='verified':blocks.append('provenance_unverified')
    resolution=card.get('expert_resolution',{})
    resolved=bool(resolution.get('reason') and resolution.get('questions_digest')==checksum(
        [card.get('ambiguities',[]),card.get('condition_review',{})]))
    if card.get('condition_review',{}).get('status')=='needs_review' and not resolved:blocks.append('condition_unresolved')
    if card.get('ambiguities') and not resolved:blocks.append('semantic_ambiguity')
    if any(d.get('unresolved') for d in card.get('dependencies',[])):blocks.append('dependency_unresolved')
    if validation.get('completeness',{}).get('semantic') not in ('verified','verified_simple','model_reviewed'):
        blocks.append('critical_context_incomplete')
    status=card.get('expert_status','unreviewed')
    current=bool(status=='confirmed' and approval and approval.get('context_digest')==current_context)
    return dict(policy=VERSION,expert_status=status,approval_current=current,
        requires_reconfirmation=status=='confirmed' and not current,
        preliminary_only=not current,blocking_reasons=blocks,context_digest=current_context,
        approval=copy.deepcopy(approval) if approval else None)


def diff(old,new):
    """No similarity-based identity: ambiguous address matches stay proposals."""
    changes=[];left={x['lineage']:x for x in old};right={x['lineage']:x for x in new};pairs=[]
    for identity in sorted(left.keys()&right.keys()):pairs.append((left.pop(identity),right.pop(identity),'lineage'))
    # Unique equal meaning within an explicit source family can expose renumbering.
    for key in ('semantic','locator'):
        a=defaultdict(list);b=defaultdict(list)
        for x in left.values():a[(x['source_family'],x[key])].append(x)
        for x in right.values():b[(x['source_family'],x[key])].append(x)
        for match in sorted(a.keys()&b.keys()):
            if len(a[match])==len(b[match])==1:
                x,y=a[match][0],b[match][0];pairs.append((x,y,'same_meaning' if key=='semantic' else 'address_candidate'))
                left.pop(x['lineage']);right.pop(y['lineage'])
    for a,b,basis in pairs:
        kinds=[]
        if a['semantic']!=b['semantic']:kinds.append('meaning_changed')
        if a['locator']!=b['locator']:kinds.append('renumbered')
        if a['context']!=b['context']:kinds.append('context_changed')
        if a.get('trust')!=b.get('trust'):kinds.append('trust_changed')
        if kinds:changes.append(dict(kind='changed',changes=kinds,old=a,new=b,match_basis=basis,
            match_requires_review=basis=='address_candidate',affected_profiles=sorted(set(a['profiles']+b['profiles'])),
            confirmation_invalidated=bool(a.get('trust',{}).get('approval_current') and not b.get('trust',{}).get('approval_current'))))
    changes.extend(dict(kind='removed',old=x,new=None,affected_profiles=x['profiles']) for x in left.values())
    changes.extend(dict(kind='added',old=None,new=x,affected_profiles=x['profiles']) for x in right.values())
    return sorted(changes,key=lambda x:(x['kind'],(x.get('new') or x['old'])['lineage']))


def effective_refs(records):
    policies=[r['payload'] for r in records.values() if r['kind']=='publication_policy']
    if len(policies)>1:raise ValueError('Multiple publication policies')
    return ({tuple(x) for x in policies[0]['effective_refs']} if policies else None)
