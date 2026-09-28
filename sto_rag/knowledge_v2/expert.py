"""No LLM calls: immutable expert drafts and source-backed review decisions."""
import copy
import json
import time
import uuid
from .store import checksum, encode, Conflict

TYPES={'requirement','recommendation','permission','assumption','constraint','definition'}
EDITABLE={'description','entity_type','conditions','exceptions','applicability_note','term','glossary_kind'}


def edited(base, patch):
    if not isinstance(patch,dict) or set(patch)-EDITABLE:raise ValueError('Unsupported expert fields')
    card=copy.deepcopy(base)
    for name,value in patch.items():
        if name in ('conditions','exceptions'):
            if not isinstance(value,list) or len(value)>50:raise ValueError('Conditions list')
            clean=[]
            for row in value:
                if set(row)!={'text','citation_index'} or not isinstance(row['text'],str) or not 1<=len(row['text'].strip())<=6000:raise ValueError('Condition fields')
                index=row['citation_index']
                if type(index) is not int or not 0<=index<len(card['citations']):raise ValueError('Select source evidence')
                clean.append(dict(kind='expert',text=row['text'].strip(),citation=card['citations'][index]))
            card[name]=clean
        else:
            if not isinstance(value,str) or len(value)>12000:raise ValueError('Text field')
            card[name]=value.strip()
    if not card.get('description') or card.get('entity_type') not in TYPES:raise ValueError('Description/type required')
    if card['entity_type']=='definition':
        from .glossary import identity
        identity(card)
    if 'description' in patch and len(card.get('obligations',[]))==1:
        card['obligations'][0]['description']=card['description']
        card['composition']={'atom':[card['description']]}
    if card['entity_type']!=base.get('entity_type'):
        card['modality']={'requirement':'mandatory','constraint':'mandatory','recommendation':'recommended','permission':'permitted','assumption':'unknown','definition':'unknown'}[card['entity_type']]
        for atom in card.get('obligations',[]):atom['modality']=card['modality']
    card['expert_status']='unreviewed';card['expert_approved']=False
    card.pop('expert_resolution',None);card.pop('expert_approval',None)
    return card


def context(store,db,sid,base):
    rows=[]
    for rid,version in base.get('fragment_refs',[]):
        row=db.execute('SELECT payload FROM records WHERE set_id=? AND id=? AND version=?',(sid,rid,version)).fetchone()
        if not row:raise ValueError('Missing evidence record')
        fragment=json.loads(row[0]);text=fragment['exact_text']
        if len(text)>80000:raise ValueError('Source fragment too large for interactive view')
        structure=fragment.get('structure',{})
        rows.append(dict(locator=fragment['locator'],text=text,context_hash=fragment['context_hash'],
            structure={k:structure[k] for k in ('heading_path','heading_addresses','table_label','row','column','page','source_locator','kind') if k in structure}))
    return rows


def verify(card,contexts,source_hash):
    index={r['locator']:r for r in contexts}
    for c in card.get('citations',[]):
        f=index.get(c['locator'])
        if (not f or c.get('source_sha256',source_hash)!=source_hash or c['context_hash']!=f['context_hash']
            or type(c['start']) is not int or type(c['end']) is not int or not 0<=c['start']<c['end']<=len(f['text'])
            or f['text'][c['start']:c['end']]!=c['quote']):raise ValueError('Source citation mismatch')
    if not card.get('citations'):raise ValueError('No source evidence')


def apply(store,command_id,payload,authorize):
    sid=payload['set_id'];action=payload['action'];actor=payload['actor_id']
    if not authorize(actor,sid,'read' if action=='inspect' else 'review'):raise PermissionError('Expert permission revoked')
    old=store.command_result(command_id,'expert.apply',payload)
    if old:return old
    if action not in ('inspect','edit','confirm','reject','split','merge','refine','create'):raise ValueError('Expert action')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT digest,result FROM inbox WHERE id=?',(command_id,)).fetchone()
        if previous:
            if previous['digest']!=checksum(dict(kind='expert.apply',payload=payload)):raise Conflict('Command reuse')
            return json.loads(previous['result'])
        loaded=[]
        for ref in payload['cards']:
            latest=db.execute("SELECT version,payload FROM records WHERE set_id=? AND kind='expert_card' AND id=? ORDER BY version DESC LIMIT 1",(sid,ref['id'])).fetchone()
            original=db.execute("SELECT payload FROM records WHERE set_id=? AND id=? AND version=1 AND kind IN ('requirement','term_definition')",(sid,ref['base_id'])).fetchone()
            if not original:raise ValueError('Unknown canonical card')
            base=json.loads(original[0]);source_id=base['source_revision'][0]
            source=json.loads(db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=?",(source_id,sid)).fetchone()[0])
            if source_id!=payload['source_id']:raise ValueError('Cross-source operation')
            version=latest['version'] if latest else 1
            if version!=ref['revision']:raise Conflict('Expert version changed')
            state=json.loads(latest['payload']) if latest else dict(card=base['card'],base_ref=[ref['base_id'],1],source_revision=[source_id,1])
            if state.get('status') in ('superseded','rejected') and action in ('confirm','split','merge','refine'):raise Conflict('Inactive card')
            evidence=context(store,db,sid,base)
            for extra in state.get('extra_base_refs',[]):
                other=db.execute('SELECT payload FROM records WHERE id=? AND version=? AND set_id=?',(*extra,sid)).fetchone()
                if not other:raise ValueError('Missing merged source')
                evidence.extend(context(store,db,sid,json.loads(other[0])))
            evidence=list({r['locator']:r for r in evidence}.values())
            if len(encode(evidence).encode('utf8'))>900000:raise ValueError('Interactive evidence exceeds transfer limit')
            verify(state['card'],evidence,source['sha256'])
            loaded.append((ref,state,evidence))
        if not loaded or len(loaded)>10:raise ValueError('Select cards')
        if action!='merge' and len(loaded)!=1:raise ValueError('One card required')
        reason=payload.get('reason','').strip()
        if action!='inspect' and not 5<=len(reason)<=2000:raise ValueError('Review reason required')
        updates=[]
        def save(ref,state,card,status,**extra):
            version=ref['revision']+1
            row=dict(state,card=card,status=status,actor_id=actor,reason=reason,action=action,**extra)
            card['expert_status']=status;card['expert_approved']=status=='confirmed'
            store.put_record(sid,'expert_card',ref['id'],version,row,refs=[row['base_ref'],*row.get('extra_base_refs',[])],_db=db)
            updates.append(dict(id=ref['id'],base_id=ref['base_id'],revision=version,payload=card,status=status,
                                action=action,reason=reason,lineage=extra.get('lineage',[])))
        ref,state,evidence=loaded[0]
        if action=='inspect':
            updates=[dict(id=ref['id'],revision=ref['revision'],contexts=evidence)]
        elif action=='create':
            if not payload.get('profile_id'):raise ValueError('Choose profile')
            if len(state['card'].get('obligations',[]))!=1 and state['card'].get('entity_type')!='definition':raise ValueError('Choose an atomic source basis')
            card=edited(state['card'],dict(payload.get('patch') or {},description=payload['description']))
            card['local_profile']=payload['profile_id'];card['profile_id']='local:'+payload['profile_id']
            child=dict(ref,id=str(uuid.uuid5(uuid.UUID(command_id),'manual-requirement')),revision=0)
            save(child,state,card,'unreviewed',lineage=[ref['id']],approval=None)
        elif action=='edit':save(ref,state,edited(state['card'],payload['patch']),'unreviewed')
        elif action in ('confirm','reject'):
            updated=copy.deepcopy(state['card']);approval=None
            if action=='confirm':
                if state['card'].get('validation',{}).get('provenance',{}).get('status') not in ('pass','verified','valid'):
                    raise ValueError('Provenance unresolved')
                if state['card'].get('condition_review',{}).get('status')=='needs_review' and not payload.get('acknowledge_questions'):
                    raise ValueError('Resolve or explicitly review open questions')
                if payload.get('resolution_reason'):
                    resolution=payload['resolution_reason']
                    if not isinstance(resolution,str) or not 20<=len(resolution.strip())<=4000:raise ValueError('Explain resolved questions')
                    updated['expert_resolution']=dict(reason=resolution.strip(),questions_digest=checksum(
                        [updated.get('ambiguities',[]),updated.get('condition_review',{})]),actor_id=actor)
                if payload.get('context_digest'):
                    if not isinstance(payload['context_digest'],str) or len(payload['context_digest'])!=64:raise ValueError('Review context')
                    approval=dict(actor_id=actor,at=time.time(),version=ref['revision']+1,context_digest=payload['context_digest'])
                    updated['expert_approval']=approval
            save(ref,state,updated,'confirmed' if action=='confirm' else 'rejected',approval=approval,
                 acknowledged_questions=bool(payload.get('acknowledge_questions')))
        elif action=='refine':
            from .curation import semantic_hash
            from .applicability import validate_expression
            relation=payload['refinement_kind'];profile=payload['profile_id'];basis=payload['basis_index']
            if relation not in ('clarifies','additional_constraint','exception','conflict'):raise ValueError('Refinement relation')
            if type(basis) is not int or not 0<=basis<len(state['card']['citations']):raise ValueError('Refinement source basis')
            if len(state['card'].get('obligations',[]))!=1:raise ValueError('Split compound requirement before local refinement')
            condition=payload.get('condition')
            if relation=='exception':validate_expression(condition)
            card=edited(state['card'],dict(description=payload['description']))
            card['local_profile']=profile;card['profile_id']='local:'+profile
            card['refinement']=dict(kind=relation,parent=ref['id'],parent_version=ref['revision'],
                parent_semantic=semantic_hash(state['card']),profile_id=profile,condition=condition,reason=reason,
                basis=state['card']['citations'][basis])
            child=dict(ref,id=str(uuid.uuid5(uuid.UUID(command_id),'refinement')),revision=0)
            save(child,state,card,'unreviewed',lineage=[ref['id']],approval=None)
        elif action=='split':
            parts=payload['parts']
            if not isinstance(parts,list) or not 2<=len(parts)<=8:raise ValueError('2 to 8 parts required')
            manual=len(state['card']['obligations'])==1
            if manual and payload.get('split_logic') not in ('all_of','any_of'):raise ValueError('Explicit logical composition required')
            for i,part in enumerate(parts):
                child=dict(ref,id=str(uuid.uuid5(uuid.UUID(command_id),'split:'+str(i))),revision=0)
                card=edited(state['card'],dict(description=part['description']))
                selected=part['obligation_indices']
                if not isinstance(selected,list) or not selected or any(type(x) is not int or not 0<=x<len(card['obligations']) for x in selected):raise ValueError('Select atoms')
                card['obligations']=[card['obligations'][x] for x in selected]
                if len(selected)==1:card['obligations'][0]['description']=part['description']
                card['composition']={'atom':[part['description']]} if len(selected)==1 else state['card'].get('composition',{})
                card['composition_group']=dict(parent=ref['id'],logic={payload['split_logic']:[p['description'] for p in parts]} if manual else state['card'].get('composition',{}),manual_decomposition=manual)
                save(child,state,card,'unreviewed',lineage=[ref['id']])
            used=[i for p in parts for i in p['obligation_indices']]
            expected=[0]*len(parts) if manual else list(range(len(state['card']['obligations'])))
            if sorted(used)!=expected:raise ValueError('Split must preserve every atom exactly once')
            save(ref,state,copy.deepcopy(state['card']),'superseded',lineage=[x['id'] for x in updates])
        elif action=='merge':
            if len(loaded)<2:raise ValueError('Select at least two cards')
            if len({(x[1]['card'].get('profile_id'),x[1]['card'].get('entity_type'),x[1]['card'].get('modality')) for x in loaded})!=1:raise ValueError('Merge scope/type differs')
            if any('any_of' in s['card'].get('composition',{}) or 'unknown' in s['card'].get('composition',{}) or s['card'].get('composition_group') for _,s,_ in loaded):raise ValueError('Alternative groups need explicit logical review')
            # No weakening: all atoms, conditions and exceptions survive the merge.
            card=edited(state['card'],dict(description=payload['description']))
            for key in ('citations','obligations','conditions','exceptions','dependencies'):
                card[key]=list({checksum(row):row for _,s,_ in loaded for row in s['card'].get(key,[])}.values())
            card['composition']={'all_of':[s['card']['description'] for _,s,_ in loaded]};card['merged_base_refs']=[s['base_ref'] for _,s,_ in loaded]
            child=dict(ref,id=str(uuid.uuid5(uuid.UUID(command_id),'merge')),revision=0)
            all_refs=[r for _,s,_ in loaded for r in [s['base_ref'],*s.get('extra_base_refs',[])]]
            merged=dict(state,extra_base_refs=[list(r) for r in sorted(set(map(tuple,all_refs))) if list(r)!=state['base_ref']])
            save(child,merged,card,'unreviewed',lineage=[r['id'] for r,_,_ in loaded])
            for r,s,_ in loaded:save(r,s,copy.deepcopy(s['card']),'superseded',lineage=[child['id']])
        result=dict(kind='expert.apply.done',set_id=sid,action=action,updates=updates)
        db.execute('INSERT INTO inbox VALUES(?,?,?,?)',(command_id,checksum(dict(kind='expert.apply',payload=payload)),encode(result),time.time()))
        return result
