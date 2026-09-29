"""Small declarative, three-valued applicability language. No executable expressions."""
from .store import checksum

OPERATIONS={'presence','structure','range','units','comparison','required_fields','traceability','semantic'}
STATES={'applicable','not_applicable','unknown'}


def validate_expression(expression,depth=0):
    if depth>12 or not isinstance(expression,dict) or len(expression)!=1:raise ValueError('Applicability expression')
    op,value=next(iter(expression.items()))
    if op in ('all_of','any_of'):
        if not isinstance(value,list) or not 1<=len(value)<=100:raise ValueError('Expression operands')
        for child in value:validate_expression(child,depth+1)
    elif op=='not':validate_expression(value,depth+1)
    elif op=='fact':
        if (not isinstance(value,dict) or set(value)!={'name','in'} or not isinstance(value['name'],str)
            or not isinstance(value['in'],list) or not value['in'] or len(value['in'])>100
            or any(not isinstance(v,(str,bool,int,float)) for v in value['in'])):raise ValueError('Fact predicate')
    elif op=='unknown':
        if not isinstance(value,str) or not value:raise ValueError('Unknown needs a reason')
    else:raise ValueError('Unregistered predicate')


REVIEW_SCOPE_POLICY = 'selected-sto-default-inclusion-v1'


def evaluate(expression,facts,verify_evidence=None,*,review_scope=False):
    """Facts must carry provenance. Absence of a fact cannot prove non-applicability."""
    validate_expression(expression)
    def visit(node):
        op,value=next(iter(node.items()))
        # This is a review policy, not an extracted document fact. Only the
        # global STO scope gates are relaxed; local conditions stay three-valued.
        if review_scope and op=='not' and value.get('fact',{}).get('name')=='excluded_system_class':
            excluded=evaluate(value,facts,verify_evidence)
            if excluded['result']=='unknown':
                return True,[dict(policy=REVIEW_SCOPE_POLICY,default_inclusion=True,
                    reason='No evidenced exclusion; selected STO applies by default')],[]
        if op=='unknown':return None,[],[value]
        if op=='fact':
            if review_scope and value['name']=='organization':
                return True,[dict(policy=REVIEW_SCOPE_POLICY,default_inclusion=True,
                    reason='Selected STO applies irrespective of organization')],[]
            fact=facts.get(value['name'])
            if (not isinstance(fact,dict) or 'value' not in fact or not isinstance(fact.get('evidence'),list)
                or not fact['evidence'] or not all(isinstance(x,dict) and x.get('locator') and x.get('source') for x in fact['evidence'])):
                return None,[],['Missing evidenced fact: '+value['name']]
            if verify_evidence is None or not all(verify_evidence(value['name'],fact['value'],e) for e in fact['evidence']):
                return None,[],['Unverified fact provenance: '+value['name']]
            if isinstance(fact['value'],list):
                matches=any(type(item) is type(expected) and item==expected for item in fact['value'] for expected in value['in'])
                if not matches and fact.get('complete') is not True:return None,[],['Incomplete collection: '+value['name']]
            else:matches=any(type(fact['value']) is type(expected) and fact['value']==expected for expected in value['in'])
            return matches,[{'fact':value['name'],'value':fact['value'],'evidence':fact['evidence']}],[]
        if op=='not':
            outcome,evidence,missing=visit(value)
            return (None if outcome is None else not outcome),evidence,missing
        rows=[visit(x) for x in value]
        decisive=False if op=='all_of' else True
        for result,evidence,missing in rows:
            if result is decisive:return result,evidence,[]
        evidence=[e for _,ev,_ in rows for e in ev];missing=[m for _,_,mi in rows for m in mi]
        return (None if any(r[0] is None for r in rows) else not decisive),evidence,missing
    result,evidence,missing=visit(expression)
    return dict(result='unknown' if result is None else 'applicable' if result else 'not_applicable',
                evidence=evidence,missing=missing,criteria=expression,facts_hash=checksum(facts))


def validate_profile(profile):
    if (not isinstance(profile,dict) or set(profile)-{'id','name','expression','basis','version','kind','inherits','state','expert_status','key'}
        or not all(k in profile for k in ('id','name','expression','basis','version'))
        or not isinstance(profile['basis'],list) or not profile['basis']):raise ValueError('Profile needs source basis')
    validate_expression(profile['expression'])
    if type(profile['version']) is not int or profile['version']<1:raise ValueError('Profile version')
    if any(not isinstance(profile[k],str) or not profile[k] for k in ('id','name')):raise ValueError('Profile identity')
    if not isinstance(profile.get('inherits',[]),list) or any(not isinstance(p,str) or not p for p in profile.get('inherits',[])):
        raise ValueError('Profile parents')
    return profile


def select_requirements(profiles,cards,profile_id):
    """Union of selected profile DAGs, deduplicated by card ID. Never overrides a rule."""
    index={p['id']:validate_profile(p) for p in profiles};seen=set();visiting=set()
    def include(pid):
        if pid in visiting:raise ValueError('Cyclic profile inheritance')
        if pid in seen:return
        if pid not in index:raise ValueError('Unknown inherited profile')
        visiting.add(pid)
        for parent in index[pid].get('inherits',[]):include(parent)
        visiting.remove(pid);seen.add(pid)
    selected=[profile_id] if isinstance(profile_id,str) else profile_id
    if not isinstance(selected,list) or not selected:raise ValueError('Select one or more profiles')
    for pid in selected:include(pid)
    result={}
    for card in cards:
        memberships=card.get('profile_ids',[card.get('effective_profile_id')])
        if set(memberships)&seen and card['state'] not in ('example','definition'):result.setdefault(card['id'],card)
    return list(result.values())


def match_profiles(profiles,facts,verify_evidence=None,*,review_scope=False):
    """Independent facets may all apply; inherited predicates must also hold."""
    index={p['id']:validate_profile(p) for p in profiles}
    def expression(pid,seen):
        if pid in seen:raise ValueError('Cyclic profile inheritance')
        if pid not in index:raise ValueError('Unknown inherited profile')
        p=index[pid];terms=[p['expression']]
        terms.extend(expression(parent,seen|{pid}) for parent in p.get('inherits',[]))
        return {'all_of':terms} if len(terms)>1 else terms[0]
    return {pid:evaluate(expression(pid,set()),facts,verify_evidence,review_scope=review_scope) for pid in index}
