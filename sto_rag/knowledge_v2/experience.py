"""Curated, scoped experience. Examples never replace normative authority."""
import json
import time
import uuid
from .store import checksum, encode, Conflict, NotReady
from .applicability import evaluate, validate_expression

KINDS = {'private_example','project_agreement','norm_interpretation','extraction_error','norm_change','unsubstantiated'}
STAGES = {'language','logic','sto','inter','cross','check','verify'}
VERSION = 'curated-experience-v1'


def initialize(store):
    with store.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS experience_state(id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(id,version))')
        db.execute('CREATE TABLE IF NOT EXISTS experience_events(id TEXT PRIMARY KEY,record_id TEXT NOT NULL,version INTEGER NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL)')


def validate_draft(draft):
    required={'summary','conditions','counterexample','counter_conditions','required_evidence','stages','scope_id','normative_refs','sharing_confirmed'}
    if not isinstance(draft,dict) or not required<=set(draft) or set(draft)-required-{'conflicts_with'}:raise ValueError('Complete experience draft required')
    for ref in draft.get('conflicts_with',[]):
        if not isinstance(ref,list) or len(ref)!=2 or not isinstance(ref[0],str) or type(ref[1]) is not int:raise ValueError('Conflict reference')
    for key in ('summary','counterexample','required_evidence','scope_id'):
        if not isinstance(draft[key],str) or not 1<=len(draft[key].strip())<=4000:raise ValueError('Experience text bounds')
    if not isinstance(draft['stages'],list) or not draft['stages'] or not set(draft['stages'])<=STAGES:raise ValueError('Explicit stage scope required')
    validate_expression(draft['conditions']);validate_expression(draft['counter_conditions'])
    if draft['conditions']==draft['counter_conditions']:raise ValueError('Counterexample must distinguish applicability')
    if type(draft['sharing_confirmed']) is not bool:raise ValueError('Explicit sharing decision required')
    if not isinstance(draft['normative_refs'],list) or not 1<=len(draft['normative_refs'])<=20:raise ValueError('Normative revisions required')
    for ref in draft['normative_refs']:
        if not isinstance(ref,dict) or set(ref)!={'set_id','release_id','requirement_ref'}:raise ValueError('Normative reference schema')
        if not isinstance(ref['requirement_ref'],list) or len(ref['requirement_ref'])!=2 or type(ref['requirement_ref'][1]) is not int:raise ValueError('Requirement version')
    return draft


def consent_preserved(original,curated):
    """A curator may narrow a lesson or add exclusions, never broaden consent."""
    required=original['conditions'];excluded=original['counter_conditions']
    conditions=curated['conditions'];counter=curated['counter_conditions']
    narrower=conditions==required or required in conditions.get('all_of',[])
    more_exclusions=counter==excluded or excluded in counter.get('any_of',[])
    return narrower and more_exclusions and set(curated['stages'])<=set(original['stages'])


def verify_norms(store,draft,authorize):
    from .review import release_records
    for ref in draft['normative_refs']:
        manifest,records=release_records(store,ref['release_id'],authorize)
        if manifest['set_id']!=ref['set_id'] or records.get(tuple(ref['requirement_ref']),{}).get('kind')!='requirement':
            raise Conflict('Experience refers to another normative revision')


def apply(store,command_id,kind,payload,authorize):
    """Only authenticated bridge commands; authorization is checked on retries too.

    authorize(actor,set_id,action) is always provided by the trusted portal adapter.
    Original private documents stay local, the shared record contains only the
    explicitly reviewed generalization. Each command+event is committed atomically.
    """
    initialize(store)
    actor,sid=payload['actor_id'],payload['set_id']
    action='review' if kind=='review.submit' else 'publish'
    if not authorize(actor,sid,action):raise PermissionError('Experience grant required')
    previous=store.command_result(command_id,kind,payload)
    if previous:return previous
    pid=payload['proposal_id'];now=time.time()
    draft=None;proposal=None
    if kind=='review.submit':
        from .review import ReviewRunner
        if payload['proposal_kind'] not in KINDS:raise ValueError('Proposal classification')
        if not isinstance(payload['comment'],str) or not 10<=len(payload['comment'].strip())<=12000:raise ValueError('Reasoned review required')
        draft=validate_draft(payload['draft'])
        verify_norms(store,draft,lambda set_id:authorize(actor,set_id,'read'))
        report=ReviewRunner(store,None,lambda set_id:authorize(actor,set_id,'read'),owner=actor).report(payload['task_id'])
        finding=next((d for d in report['decisions'] if d['obligation']['id']==payload['obligation_id']),None)
        if not finding:raise ValueError('Original finding unavailable')
        norm=finding['obligation']
        if not any(r['release_id']==norm['release_id'] and r['requirement_ref']==norm['requirement_ref'] for r in draft['normative_refs']):
            raise ValueError('Review does not refer to finding normative version')
        with store.connection() as db:
            task=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(payload['task_id'],)).fetchone()[0])
        blocks={b['id']:b for d in task['documents'] for b in d['blocks']}
        evidence=payload['evidence']
        if not isinstance(evidence,list) or not evidence:raise ValueError('Document evidence required')
        for e in evidence:
            b=blocks.get(e.get('block_id')) if isinstance(e,dict) else None
            if not b or not isinstance(e.get('quote'),str) or not e['quote'].strip() or e['quote'] not in b['text']:raise ValueError('Review evidence not in original document revision')
        proposal=dict(portal_review_id=pid,scope_id=draft['scope_id'],evidence=evidence,author=actor,
            finding=dict(task_id=payload['task_id'],obligation_id=payload['obligation_id'],state=finding['state'],documents=report['snapshot']['versions']['documents']),
            comment=payload['comment'],proposal_kind=payload['proposal_kind'],draft=draft,created=now)
    elif kind=='review.approve':
        draft=validate_draft(payload['draft'])
        verify_norms(store,draft,lambda set_id:authorize(actor,set_id,'read'))
    elif kind=='review.repair':
        return repair(store,command_id,payload,authorize)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT digest,result FROM inbox WHERE id=?',(command_id,)).fetchone()
        digest=checksum(dict(kind=kind,payload=payload))
        if old:
            if old['digest']!=digest:raise Conflict('Review command collision')
            return json.loads(old['result'])
        scope=db.execute('SELECT scope_id FROM sets WHERE id=?',(sid,)).fetchone()
        if not scope:raise NotReady('Experience set must be registered')
        if draft and draft['scope_id']!=scope['scope_id']:raise Conflict('Cannot widen experience scope')
        rid=pid;version=1;state='pending'
        if kind=='review.submit':
            store.put_record(sid,'review_proposal',pid,1,proposal,_db=db)
        elif kind=='review.approve':
            record=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='review_proposal'",(pid,sid)).fetchone()
            if not record:raise NotReady('Proposal not ingested')
            proposal=json.loads(record[0])
            rejected=db.execute("SELECT state FROM experience_state WHERE id=? AND version=1",(pid,)).fetchone()
            if rejected and rejected['state']=='rejected':raise NotReady('Rejected proposal requires a new submission')
            if proposal['proposal_kind'] in ('extraction_error','norm_change','unsubstantiated'):
                raise NotReady('Requires corrected source/card or sufficient justification; cannot activate an example')
            # Approval cannot silently broaden what the author offered for sharing.
            if draft['scope_id']!=proposal['scope_id'] or not draft['sharing_confirmed'] or not proposal['draft']['sharing_confirmed']:
                raise PermissionError('Author and curator must explicitly confirm sharing')
            if not consent_preserved(proposal['draft'],draft):
                raise PermissionError('Curator cannot broaden author conditions, exclusions or stages')
            rid=payload.get('experience_id') or str(uuid.uuid5(uuid.NAMESPACE_URL,'experience:'+pid))
            version=payload.get('version',1)
            if type(version) is not int or version<1:raise ValueError('Experience version')
            last=db.execute('SELECT version,state FROM experience_state WHERE id=? ORDER BY version DESC LIMIT 1',(rid,)).fetchone()
            if version!=(last['version']+1 if last else 1):raise Conflict('Experience revision changed')
            for conflict in draft.get('conflicts_with',[]):
                active=db.execute("SELECT s.state FROM experience_state s JOIN records r ON r.id=s.id AND r.version=s.version WHERE s.id=? AND s.version=? AND r.set_id=?",(*conflict,sid)).fetchone()
                if not active:raise ValueError('Conflict reference outside experience scope')
                if active['state']=='approved':raise NotReady('Resolve the conflicting approved experience first')
            previous_record=db.execute('SELECT set_id FROM records WHERE id=? LIMIT 1',(rid,)).fetchone()
            if previous_record and previous_record['set_id']!=sid:raise PermissionError('Cannot supersede another scope')
            lesson=dict(draft,proposal_ref=[pid,1],approval_event_id=command_id,curator=actor,
                trust='curator_approved',summary=draft['summary'],source_priority='example_only',
                supersedes=[rid,version-1] if last else None,proposal_kind=proposal['proposal_kind'])
            record_kind='clarification' if proposal['proposal_kind']=='norm_interpretation' else 'review_case'
            store.put_record(sid,record_kind,rid,version,lesson,_db=db)
            if last:db.execute("UPDATE experience_state SET state='superseded',event_id=? WHERE id=? AND version=?",(command_id,rid,last['version']))
            state='approved'
            db.execute('INSERT INTO experience_state VALUES(?,?,?,?)',(rid,version,state,command_id))
        elif kind in ('review.revoke','review.reject'):
            rid=payload.get('experience_id') or pid;version=payload.get('version',1)
            if not isinstance(payload.get('reason'),str) or not payload['reason'].strip():raise ValueError('Reason required')
            rec=db.execute('SELECT kind FROM records WHERE id=? AND version=? AND set_id=?',(rid,version,sid)).fetchone()
            if not rec:raise NotReady('Experience record unavailable')
            if kind=='review.revoke' and rec['kind'] not in ('review_case','clarification'):raise ValueError('Only an experience version can be revoked')
            if kind=='review.reject' and rec['kind']!='review_proposal':raise ValueError('Reject an unapproved proposal, revoke an approved experience')
            if kind=='review.reject':
                linked=db.execute("SELECT r.payload FROM records r JOIN experience_state s ON s.id=r.id AND s.version=r.version WHERE r.set_id=? AND r.kind IN ('review_case','clarification') AND s.state='approved'",(sid,))
                if any(json.loads(r['payload']).get('proposal_ref')==[pid,1] for r in linked):
                    raise Conflict('Revoke approved experience before rejecting its proposal')
            state='rejected' if kind=='review.reject' else 'revoked'
            db.execute('INSERT INTO experience_state VALUES(?,?,?,?) ON CONFLICT(id,version) DO UPDATE SET state=excluded.state,event_id=excluded.event_id',(rid,version,state,command_id))
        else:raise ValueError('Unsupported review command')
        result=dict(kind=kind+'.done',set_id=sid,proposal_id=pid,record_id=rid,version=version,state=state)
        db.execute('INSERT INTO experience_events VALUES(?,?,?,?,?,?,?)',(command_id,rid,version,kind,str(actor),payload.get('reason','Explicit review/curation'),now))
        db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,digest,encode(result),now))
        store._event(db,'reply:'+command_id,result['kind'],sid,result)
        return result


def repair(store,command_id,payload,authorize):
    """Correct a derived card, never the normative original; publication is separate."""
    from .review import release_records
    from .norm_validation import provenance, completeness
    actor,sid=payload['actor_id'],payload['set_id']
    with store.connection() as db:
        proposal=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='review_proposal'",(payload['proposal_id'],sid)).fetchone()
    if not proposal:raise NotReady('Review not ingested')
    proposal=json.loads(proposal[0])
    if proposal['proposal_kind']!='extraction_error':raise ValueError('Not a card correction review')
    target=payload['normative_ref']
    if target not in proposal['draft']['normative_refs']:raise Conflict('Correction target outside reviewed norms')
    if not authorize(actor,target['set_id'],'publish'):raise PermissionError('Normative curator required too')
    manifest,records=release_records(store,target['release_id'],lambda s:authorize(actor,s,'read'))
    key=tuple(target['requirement_ref']);old=records[key]['payload'];card=payload['corrected_card']
    if not isinstance(card,dict) or card.get('id')!=key[0]:raise ValueError('Card identity')
    if any(card.get(k)!=old.get('card',{}).get(k) for k in ('modality','conditions','exceptions','dependencies','applicability')):
        raise ValueError('A review cannot change source modality, conditions or exceptions')
    fragments={}
    for ref in old['fragment_refs']:
        f=records[tuple(ref)]['payload']
        source=records[tuple(f['source_revision'])]['payload']
        fragments[f['locator']]=dict(f,source_sha256=source['sha256'])
    check=provenance(card,fragments)
    if check['status']!='verified':raise ValueError('Corrected card has ungrounded quotes or obligations')
    complete=completeness(card,fragments,card.get('dependencies',[]))
    card=dict(card,validation=dict(provenance=check,completeness=complete,applicability={'result':'unknown'}),
              state='validated' if complete.get('semantic')=='verified_simple' else 'needs_review')
    revised=dict(old,card=card,correction_review=[payload['proposal_id'],1],correction_event=command_id)
    version=key[1]+1
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        cached=db.execute('SELECT digest,result FROM inbox WHERE id=?',(command_id,)).fetchone()
        if cached:
            if cached['digest']!=checksum(dict(kind='review.repair',payload=payload)):raise Conflict('Command identity changed')
            return json.loads(cached['result'])
        latest=db.execute('SELECT max(version) FROM records WHERE id=?',(key[0],)).fetchone()[0]
        if latest!=key[1]:raise Conflict('Card revision changed')
        store.put_record(target['set_id'],'requirement',key[0],version,revised,_db=db)
        refs=[[key[0],version]]
        for i,atom in enumerate(card['obligations']):
            oid=str(uuid.uuid5(uuid.NAMESPACE_URL,f'corrected:{key[0]}:{version}:{i}'))
            store.put_record(target['set_id'],'obligation',oid,1,dict(atom,requirement_ref=[key[0],version]),_db=db)
            refs.append([oid,1])
        result=dict(kind='review.repair.done',set_id=sid,proposal_id=payload['proposal_id'],record_id=key[0],version=version,
                    state='corrected_draft',corrected_set_id=target['set_id'],new_refs=refs)
        db.execute('INSERT INTO experience_events VALUES(?,?,?,?,?,?,?)',(command_id,key[0],version,'review.repair',str(actor),payload.get('reason','Correct derived extraction'),time.time()))
        db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,checksum(dict(kind='review.repair',payload=payload)),encode(result),time.time()))
        store._event(db,'reply:'+command_id,result['kind'],sid,result)
        return result


class ExperienceSelector:
    """Filters eligibility before similarity/top-k; no recent-100 shortcut."""
    def __init__(self,search,scope_id,verify_fact):
        self.search,self.scope_id,self.verify_fact=search,scope_id,verify_fact
        initialize(search.store)

    def eligible(self,release_id,stage,facts,normative_releases):
        from .review import release_records
        manifest,records=release_records(self.search.store,release_id,self.search.authorize)
        result={}
        with self.search.store.connection() as db:
            for key,record in records.items():
                if record['kind'] not in ('review_case','clarification'):continue
                item=record['payload']
                state=db.execute('SELECT state FROM experience_state WHERE id=? AND version=?',key).fetchone()
                if not state or state['state']!='approved' or item.get('trust')!='curator_approved':continue
                if item['scope_id']!=self.scope_id or stage not in item['stages']:continue
                if any(r['release_id'] not in normative_releases for r in item['normative_refs']):continue
                if evaluate(item['conditions'],facts,self.verify_fact)['result']!='applicable':continue
                if evaluate(item['counter_conditions'],facts,self.verify_fact)['result']!='not_applicable':continue
                verify_norms(self.search.store,item,self.search.authorize)
                result[key]=item
        return result

    def select(self,releases,query,stage,facts,normative_releases,token_count,token_budget,max_examples=3):
        if not 1<=max_examples<=4 or token_budget<0:raise ValueError('Experience budget')
        candidates=[]
        for release in releases:
            eligible=self.eligible(release,stage,facts,normative_releases)
            if not eligible:continue
            hits=self.search.reference(release,query,kinds=('review_case','clarification'),
                eligible_refs=list(eligible),limit=max_examples)
            from .check_log import emit
            emit('rag',dict(release_id=release,query=query,stage=stage,eligible_refs=list(eligible),
                            limit=max_examples,token_budget=token_budget,hits=hits))
            for hit in hits:
                key=(hit['record_id'],hit['version'])
                # Revocation/ACL may happen while embeddings are being computed.
                fresh=self.eligible(release,stage,facts,normative_releases)
                if key not in fresh:continue
                item=fresh[key]
                candidates.append(dict(id=key[0],version=key[1],release_id=release,score=hit['score'],
                    summary=item['summary'],conditions=item['conditions'],counterexample=item['counterexample'],
                    required_evidence=item['required_evidence'],trust=item['trust'],source_priority='example_only'))
        chosen=[];seen=set();used=0
        for item in sorted(candidates,key=lambda r:(-r['score'],r['id'])):
            if item['id'] in seen:continue
            cost=token_count(encode(item))
            if used+cost>token_budget:continue
            chosen.append(item);seen.add(item['id']);used+=cost
            if len(chosen)==max_examples:break
        return chosen

    def related(self,release_id,draft,limit=5):
        """Curator suggestions only; vector similarity never merges conditions."""
        from .review import release_records
        validate_draft(draft)
        _,records=release_records(self.search.store,release_id,self.search.authorize)
        eligible={}
        with self.search.store.connection() as db:
            for key,record in records.items():
                item=record['payload']
                if record['kind'] not in ('review_case','clarification') or item['scope_id']!=self.scope_id:continue
                state=db.execute('SELECT state FROM experience_state WHERE id=? AND version=?',key).fetchone()
                if state and state['state']=='approved':eligible[key]=item
        if not eligible:return []
        hits=self.search.reference(release_id,draft['summary'],kinds=('review_case','clarification'),eligible_refs=list(eligible),limit=limit)
        # Revocation or permission changes during retrieval must also take effect.
        release_records(self.search.store,release_id,self.search.authorize)
        with self.search.store.connection() as db:
            active={(r['id'],r['version']) for r in db.execute("SELECT id,version FROM experience_state WHERE state='approved'")}
        hits=[h for h in hits if (h['record_id'],h['version']) in active]
        return [dict(id=h['record_id'],version=h['version'],summary=eligible[(h['record_id'],h['version'])]['summary'],
                     same_conditions=eligible[(h['record_id'],h['version'])]['conditions']==draft['conditions'],
                     decision='curator_comparison_required') for h in hits]


def publish(store,command_id,payload,encoder,vector,authorize):
    """Explicit curator publication; same FTS/Qdrant generation as other knowledge."""
    from .search import GenerationBuilder
    initialize(store)
    actor,sid=payload['actor_id'],payload['set_id']
    if not authorize(actor,sid,'publish'):raise PermissionError('Curator publication grant required')
    old=store.command_result(command_id,'experience.publish',payload)
    if old:return old
    rid=str(uuid.uuid5(uuid.NAMESPACE_URL,'experience-release:'+command_id))
    with store.connection() as db:
        existing=db.execute('SELECT manifest FROM releases WHERE id=?',(rid,)).fetchone()
        if existing:manifest=json.loads(existing[0])
        else:
            selected=list(db.execute("SELECT r.id,r.version,r.payload FROM records r JOIN experience_state s ON s.id=r.id AND s.version=r.version WHERE r.set_id=? AND r.kind IN ('review_case','clarification') AND s.state='approved'",(sid,)))
            if not selected:raise NotReady('No curator-approved experience')
            refs={(r['id'],r['version']) for r in selected}
            queue=list(refs)
            while queue:
                key=queue.pop()
                for link in db.execute('SELECT target_id,target_version FROM record_links WHERE record_id=? AND record_version=?',key):
                    ref=tuple(link)
                    if ref not in refs:refs.add(ref);queue.append(ref)
            manifest=None
    if manifest is None:
        manifest=store.create_release(sid,rid,str(uuid.uuid5(uuid.NAMESPACE_URL,'generation:'+rid)),encoder.space.id,sorted(refs),{'experience':VERSION})
    GenerationBuilder(store,encoder,vector).build(rid)
    with store.connection() as db:
        for item in manifest['items']:
            if item['kind'] not in ('review_case','clarification'):continue
            state=db.execute('SELECT state FROM experience_state WHERE id=? AND version=?',(item['id'],item['version'])).fetchone()
            if not state or state['state']!='approved':raise NotReady('Experience was revoked/superseded during publication')
            lesson=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(item['id'],item['version'])).fetchone()[0])
            verify_norms(store,lesson,lambda set_id:authorize(actor,set_id,'read'))
    if not authorize(actor,sid,'publish'):raise PermissionError('Publication grant revoked')
    att=dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(manifest),
             embedding_space=encoder.space.id,record_count=len(manifest['items']),watermark=1)
    store.attest_ready(rid,att)
    store.apply_command(command_id+':release','release.publish',dict(set_id=sid,release_id=rid,manifest_hash=checksum(manifest)))
    result=dict(kind='experience.publish.done',set_id=sid,release_id=rid,manifest=manifest,manifest_hash=checksum(manifest),attestation=att)
    return store.remember_result(command_id,'experience.publish',payload,result)


def suggest(store,command_id,payload,client,authorize):
    """One requested LLM suggestion; never an activation or expert approval."""
    from .review import release_records
    actor,sid=payload['actor_id'],payload['set_id']
    if not authorize(actor,sid,'review'):raise PermissionError('Review grant required')
    old=store.command_result(command_id,'review.suggest',payload)
    if old:return old
    with store.connection() as db:
        row=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND kind='review_proposal' AND set_id=?",(payload['proposal_id'],sid)).fetchone()
    if not row:raise NotReady('Review unavailable')
    proposal=json.loads(row[0])
    if proposal['author']!=actor and not authorize(actor,sid,'publish'):raise PermissionError('Author or curator required')
    context=[]
    for ref in proposal['draft']['normative_refs']:
        _,records=release_records(store,ref['release_id'],lambda s:authorize(actor,s,'read'))
        requirement=records[tuple(ref['requirement_ref'])]['payload']
        context.extend(records[tuple(f)]['payload'] for f in requirement['fragment_refs'])
    request=dict(stage='experience_suggestion',review=proposal['comment'],draft=proposal['draft'],evidence=proposal['evidence'],normative_context=context)
    if client.count(request)+client.output_tokens+512>client.context:raise NotReady('Advisory context exceeds budget; no truncation')
    from .model_queue import model_turn
    with model_turn(store,client):
        verify_norms(store,proposal['draft'],lambda s:authorize(actor,s,'read'))
        suggestion=client.complete(request)
    if not isinstance(suggestion,dict) or set(suggestion)!={'summary','counterexample','required_evidence'}:raise ValueError('Suggestion schema')
    if any(not isinstance(v,str) or not 1<=len(v.strip())<=4000 for v in suggestion.values()):raise ValueError('Suggestion text bounds')
    verify_norms(store,proposal['draft'],lambda s:authorize(actor,s,'read'))
    if not authorize(actor,sid,'review'):raise PermissionError('Review grant revoked')
    result=dict(kind='review.suggest.done',set_id=sid,proposal_id=payload['proposal_id'],state='pending',
                draft=dict(proposal['draft'],**suggestion),model=client.signature,expert_validation=False)
    return store.remember_result(command_id,'review.suggest',payload,result)
