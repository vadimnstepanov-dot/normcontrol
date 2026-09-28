"""Materialize an explicitly pinned expert selection; never read latest silently."""
import copy,json,uuid
from .store import checksum,Conflict,NotReady
from .curation import VERSION,fingerprint,context_digest,owners,profile_graph,semantic_hash,trust,diff
from .expert import context,verify


def materialize(store,payload,original_ids,*,analyses=None):
    selection=payload['curation'];sid=payload['set_id'];profiles=selection['profiles']
    if selection.get('version')!=VERSION or checksum(selection)!=payload['versions'].get('curation_digest'):
        raise Conflict('Pinned curation selection differs')
    index,closure=profile_graph(profiles)
    selected=set(payload['source_revisions']);loaded=[];refs=set();compiled_profiles={}
    with store.connection() as db:
        for item in selection['cards']:
            if item['source_id'] not in selected or item['base_id'] not in original_ids:raise Conflict('Unselected source/card')
            row=db.execute('SELECT payload,kind FROM records WHERE id=? AND version=1 AND set_id=?',(item['base_id'],sid)).fetchone()
            if not row or row['kind'] not in ('requirement','term_definition'):raise Conflict('Missing original card')
            base=json.loads(row['payload']);state=None
            if item['revision']>1 or item.get('derived'):
                r=db.execute("SELECT payload FROM records WHERE id=? AND version=? AND set_id=? AND kind='expert_card'",
                    (item['id'],item['revision'],sid)).fetchone()
                if not r:raise Conflict('Missing pinned expert version')
                state=json.loads(r[0]);refs.add((item['id'],item['revision']))
            card=copy.deepcopy(state['card'] if state else base['card'])
            if checksum(card)!=item['digest']:raise Conflict('Portal projection differs from canonical card')
            if base['source_revision'][0]!=item['source_id']:raise Conflict('Source mismatch')
            refs.add((item['base_id'],1))
            evidence=context(store,db,sid,base);fragment_refs=list(base['fragment_refs'])
            for extra in (state or {}).get('extra_base_refs',[]):
                r=db.execute('SELECT payload FROM records WHERE id=? AND version=? AND set_id=?',(*extra,sid)).fetchone()
                if not r:raise Conflict('Merged evidence missing')
                merged=json.loads(r[0]);evidence.extend(context(store,db,sid,merged));fragment_refs.extend(merged['fragment_refs'])
            source=json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=1',(item['source_id'],)).fetchone()[0])
            provenance=True
            try:verify(card,evidence,source['sha256'])
            except (ValueError,KeyError,TypeError):provenance=False
            loaded.append(dict(item=item,card=card,base=base,state=state,fragments=sorted(set(map(tuple,fragment_refs))),provenance=provenance,source_name=source.get('filename','')))
    if {x['base_id'] for x in selection['cards']}!=set(original_ids):raise Conflict('Selection omits extracted cards')
    fingerprints=[fingerprint(r['item']['id'],r['item']['source_id'],r['card'].get('profile_id'),
                    r['card'],r['item']['status']) for r in loaded]
    # The complete context inventory is frozen by the portal. Selected canonical
    # records must agree; context outside the selected sources is never executable.
    context_items=selection.get('context_items',fingerprints)
    expected={x['id']:x for x in context_items}
    if any(expected.get(x['id'])!=x for x in fingerprints):raise Conflict('Curation context mismatch')
    for pid in sorted(index,key=lambda p:len(closure[p])):
        p=index[pid];definition=p['definition'];parents=[compiled_profiles[x] for x in definition['parents']]
        identity=str(uuid.uuid5(uuid.UUID(sid),'profile:'+checksum([p,parents])))
        basis=[dict(source_id=b['source_id'],profile_id=b['profile_id']) for b in definition['bindings'] if b['source_id'] in selected]
        expression=copy.deepcopy(definition['expression'])
        if any(b['source_id'] not in selected for b in definition['bindings']):
            expression={'all_of':[expression,{'unknown':'В выпуск не включена часть источников профиля'}]}
        value=dict(id=identity,name=p['definition']['name'],version=p['revision'],inherits=parents,
            expression=expression,basis=basis or [{'configuration_revision':p['revision']}])
        store.put_record(sid,'profile',identity,1,dict(definition=value,source_revision=[sorted(selected)[0],1],
            portal_profile_id=pid,portal_revision=p['revision'],publication_materialized=True))
        compiled_profiles[pid]=identity;refs.add((identity,1))
    from .glossary import frozen_active
    glossary_active=frozen_active(selection,loaded)
    effective=[];catalog=[];seen={}
    from .quality import assessment,duplicate_key,counts,MODE,VERSION as QUALITY_VERSION,source_example_scopes
    examples={s:source_example_scopes(store,a) for s,a in (analyses or {}).items()}
    edge_map={}
    with store.connection() as db:
        for edge in db.execute("SELECT id,version,payload FROM records WHERE set_id=? AND kind='dependency'",(sid,)):
            dependency=json.loads(edge['payload'])
            edge_map.setdefault(tuple(dependency['from_ref']),[]).append((dict(edge),dependency))
    for row,fp in zip(loaded,fingerprints):
        item,card,base=row['item'],row['card'],row['base'];state=row['state'] or {}
        if item['status'] in ('rejected','superseded'):continue
        current=context_digest(fp,profiles,context_items)
        levels=trust(card,state.get('approval'),current,row['provenance'])
        direct=owners(fp,profiles)
        memberships=[compiled_profiles[x] for x in direct]
        # Raw extractor profiles are retained when no configurable profile exists.
        if not profiles:memberships=card.get('profile_ids') or [card.get('effective_profile_id') or card.get('profile_id')]
        memberships=[x for x in memberships if x]
        card['profile_ids']=memberships;card['effective_profile_id']=memberships[0] if memberships else None
        if not memberships:levels['blocking_reasons'].append('profile_unresolved')
        card['publication_trust']=levels;card['expert_approved']=levels['approval_current']
        quality=None
        if analyses is not None:
            gaps={g['locator'] for g in analyses[item['source_id']]['coverage_audit']['gaps']}
            gaps.update(g['locator'] for g in analyses[item['source_id']].get('source_gaps',[]) if g.get('locator'))
            quality=assessment(card,gaps,provenance=row['provenance'],profile=bool(memberships),approved=levels['approval_current'])
            marker=examples[item['source_id']].get(card.get('locator'))
            if marker and not levels['approval_current']:
                quality.update(status='candidate',reasons=quality['reasons']+['example_scope'],example_marker=marker)
            if quality['status']=='ready' and not levels['approval_current']:
                audit=analyses[item['source_id']]['quality_audit']['decisions'].get(item['base_id'])
                if not audit or audit.get('card_digest')!=item['digest'] or audit.get('status')!='ready':
                    quality.update(status='candidate',reasons=quality['reasons']+['context_audit'],
                        context_reason=(audit or {}).get('reason','Контекстный аудит этой версии карточки не выполнен'))
            if quality['status'] in ('ready','reference'):
                key=duplicate_key(card,item['source_id'])
                if key in seen:quality.update(status='duplicate',reasons=['duplicate'],duplicate_of=seen[key])
                else:seen[key]=item['id']
            card['quality']=quality
            if quality['status'] in ('candidate','duplicate'):levels['blocking_reasons'].append('quality_'+quality['status'])
        if levels['approval_current'] and not levels['blocking_reasons']:card['state']='validated'
        value=dict(base,card=card,fragment_refs=[list(x) for x in row['fragments']],publication_materialized=True,
                   modality=card.get('modality',base.get('modality','unknown')),
                   condition=card.get('applicability') or base.get('condition') or {'unknown':'Не определена применимость'},
                   curation_ref=[item['id'],item['revision']],lineage=item['id'])
        rid=str(uuid.uuid5(uuid.UUID(sid),'effective:'+checksum(value)));kind='term_definition' if card['entity_type']=='definition' else 'requirement'
        record_refs=[(item['base_id'],1),*row['fragments']]
        if row['state']:record_refs.append((item['id'],item['revision']))
        store.put_record(sid,kind,rid,1,value,refs=record_refs);refs.add((rid,1))
        active_term=kind!='term_definition' or glossary_active is None or item['id'] in glossary_active
        if active_term and (quality is None or quality['status']=='ready' or (quality['status']=='reference' and kind=='term_definition')):effective.append([rid,1])
        # Dependency edges still point to exact source versions. Reattach their
        # origin to the effective card so the review retains critical context.
        for edge,dependency in edge_map.get((item['base_id'],1),[]):
            dep_id=str(uuid.uuid5(uuid.UUID(rid),'dependency:'+edge['id']+':'+str(edge['version'])))
            store.put_record(sid,'dependency',dep_id,1,dict(dependency,from_ref=[rid,1]),refs=[(edge['id'],edge['version'])])
            refs.add((dep_id,1))
        for i,atom in enumerate(card.get('obligations',[])):
            atom_id=str(uuid.uuid5(uuid.UUID(rid),'atom:'+str(i)))
            store.put_record(sid,'obligation',atom_id,1,dict(atom,requirement_ref=[rid,1]));refs.add((atom_id,1))
        affected=sorted(p for p in index if set(direct)&closure[p])
        catalog.append(dict(kind='requirement',lineage=item['id'],ref=[rid,1],base_id=item['base_id'],revision=item['revision'],
            source_id=item['source_id'],source_family=selection['families'][item['source_id']],
            original_digest=item['digest'],locator=card.get('locator',''),description=card.get('description','')[:1500],semantic=semantic_hash(card),
            context=current,profiles=affected,profile_names=[index[p]['definition']['name'] for p in affected],
            trust=levels,quality=quality,refinement=card.get('refinement'),entity_type=card['entity_type'],
            conditions=card.get('conditions',[]),exceptions=card.get('exceptions',[]),dependencies=card.get('dependencies',[]),glossary_active=active_term if kind=='term_definition' else None,
            profile_context=[dict(name=index[p]['definition']['name'],revision=index[p]['revision'],
                expression=index[p]['definition']['expression']) for p in sorted(set().union(*(closure[x] for x in direct))) ]))
    if not any(x['entity_type']!='definition' for x in catalog):raise NotReady('No active requirements')
    from .trace import compile_links
    links=compile_links(selection,catalog,loaded)
    policy=dict(links=links,version=VERSION,effective_refs=effective,profiles=compiled_profiles,curation_digest=checksum(selection),catalog=catalog,
                profile_snapshot=profiles)
    if analyses is not None:
        totals=counts(catalog)
        if not totals.get('ready'):raise NotReady('No complete, applicable mandatory requirements passed screening')
        policy['quality']=dict(mode=MODE,version=QUALITY_VERSION,counts=totals,
            coverage_gap_count=sum(len(a['coverage_audit']['gaps']) for a in analyses.values()),
            source_gap_count=sum(len(a.get('source_gaps',[])) for a in analyses.values()),
            complete=False,analyses={sid:dict(run_id=a['run_id'],summary_digest=checksum(a['summary']),
                coverage_gaps=a['coverage_audit']['gaps'],source_gaps=a.get('source_gaps',[]),
                quality_audit_digest=a['quality_audit']['digest']) for sid,a in analyses.items()})
    identity=str(uuid.uuid5(uuid.UUID(payload['release_id']),'publication-policy'))
    store.put_record(sid,'publication_policy',identity,1,policy,refs=list(refs));refs.add((identity,1))
    return refs,policy


def material(store,release_id):
    with store.connection() as db:
        manifest=json.loads(db.execute('SELECT manifest FROM releases WHERE id=?',(release_id,)).fetchone()[0])
        policy=next((i for i in manifest['items'] if i['kind']=='publication_policy'),None)
        if not policy:return None
        return json.loads(db.execute('SELECT payload FROM records WHERE id=? AND version=?',(policy['id'],policy['version'])).fetchone()[0])


def changes(store,release_id,previous=None):
    new=material(store,release_id);old=material(store,previous) if previous else None
    rows=diff(old['catalog'] if old else [],new['catalog'])
    before={x['id']:x for x in (old or {}).get('links',[])};after={x['id']:x for x in new.get('links',[])}
    for identity in sorted(set(before)|set(after)):
        a,b=before.get(identity),after.get(identity)
        if a==b:continue
        item=b or a
        rows.append(dict(kind='trace_link_added' if not a else 'trace_link_removed' if not b else 'trace_link_changed',
            lineage=identity,description=item['description'],changes=['normative_trace'],
            old=a,new=b,affected_profiles=[],reason='Изменилась закреплённая нормативная связь.'))
    return [*new['catalog'],*(dict(row,kind='diff',change_kind=row['kind']) for row in rows)]
