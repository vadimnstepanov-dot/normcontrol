"""Build an immutable, searchable normative release from explicit source revisions."""
import json
import uuid

from .ingest import PARSER_VERSION
from .norms import EXTRACTOR_VERSION
from .search import GenerationBuilder
from .store import Conflict, NotReady, checksum


def prepare(store, command_id, payload, encoder, vector, authorize):
    if not authorize(payload['actor_id'], payload['set_id'], 'upload'):
        raise PermissionError('Upload grant required')
    previous=store.command_result(command_id,'release.prepare',payload)
    if previous:return previous
    sid=payload['set_id'];sources=payload['source_revisions']
    if not isinstance(sources,list) or not sources or len(sources)>100 or len(set(sources))!=len(sources):
        raise ValueError('Select unique source revisions')
    from .semantic import VERSION as SEMANTIC_VERSION
    from .quality import MODE,VERSION as QUALITY_VERSION
    screened=payload.get('mode')==MODE
    if payload.get('mode','complete') not in ('complete',MODE):raise ValueError('Unknown release mode')
    if screened and (not payload.get('curation') or payload['versions'].get('quality')!=QUALITY_VERSION or checksum(payload.get('analyses'))!=payload['versions'].get('analysis_selection_digest')):
        raise Conflict('Screening selection differs')
    basic={k:v for k,v in payload['versions'].items() if k not in ('curation','curation_digest','quality','analysis_selection_digest')}
    if basic not in ({'parser':PARSER_VERSION,'extractor':EXTRACTOR_VERSION},
                                   {'parser':PARSER_VERSION,'extractor':SEMANTIC_VERSION}):
        raise Conflict('Preparation versions changed')
    with store.connection() as db:
        rows=list(db.execute('SELECT id,version,kind,payload FROM records WHERE set_id=? ORDER BY id,version',(sid,)))
    by_id={}
    for row in rows:by_id.setdefault(row['id'],[]).append((row['version'],row['kind'],json.loads(row['payload'])))
    refs=set();requirements=set();selected=set(sources);semantic_runs={};semantic_cards=set();semantic_profiles=set();parse_refs={};analyses={}
    for source_id in sources:
        versions=by_id.get(source_id,[])
        if not versions or versions[-1][1]!='source_revision':raise NotReady('Source not ingested')
        if versions[-1][2]['parser_version']!=PARSER_VERSION:raise Conflict('Source parser version differs')
        refs.add((source_id,versions[-1][0]))
        # A complete extractor run is required, including its uncertainty ledger.
        folder=store.directory/'analyses'
        marker=folder/(source_id+'.complete')
        run=(payload.get('analyses',{}).get(source_id,{}).get('run_id') if screened else marker.read_text(encoding='ascii') if marker.is_file() else None)
        if not isinstance(run,str) or len(run)!=64 or any(c not in '0123456789abcdef' for c in run) or not (folder/(run+'.json')).is_file():
            raise NotReady('Source extraction has not completed')
        analysis=json.loads((folder/(run+'.json')).read_text(encoding='utf8'))
        if screened:
            if analysis['source_id']!=source_id or analysis['source_sha256']!=versions[-1][2]['sha256'] or checksum(analysis['summary'])!=payload['analyses'][source_id]['summary_digest']:raise Conflict('Pinned source analysis differs')
            coverage=analysis.get('coverage_audit',{})
            if analysis.get('version')!='semantic-9.1.3' or analysis.get('errors') or analysis.get('profile_errors') or coverage.get('accounted')!=coverage.get('fragments') or not coverage.get('fragments'):raise NotReady('Screening requires an accounted, technically completed semantic analysis')
            from .quality_audit import load as load_audit
            audited=load_audit(store,analysis)
            analyses[source_id]=dict(analysis,quality_audit=audited)
        if analysis['version']!=payload['versions']['extractor']:raise Conflict('Pinned analysis extractor differs')
        if analysis.get('version')=='semantic-9.1.3':
            if not screened and not analysis.get('complete'):raise NotReady('Semantic analysis has unresolved processing gaps')
            semantic_runs[source_id]=analysis['run_id']
            parse_refs[source_id]=analysis.get('parse_ref')
            semantic_cards.update(c['id'] for c in analysis['cards'])
            semantic_profiles.update(p['id'] for p in analysis['profiles'])
    # Human versions from an earlier analysis of the SAME immutable source remain
    # eligible. Original citations are revalidated by materialize, never inferred.
    for item in payload.get('curation',{}).get('cards',[]):
        base=by_id.get(item['base_id'],[])
        if item['revision']>1 or item.get('derived'):
            if not base or base[0][1] not in ('requirement','term_definition') or base[0][2].get('source_revision',[None])[0] not in selected:
                raise Conflict('Expert lineage outside selected source')
            semantic_cards.add(item['base_id'])
    for rid,versions in by_id.items():
        latest_version,kind,value=versions[-1]
        if value.get('publication_materialized'):continue
        origin=value.get('source_revision')
        if origin and origin[0] in selected and kind in ('fragment','structured_fragment','profile','requirement','term_definition'):
            if origin[0] in semantic_runs:
                if kind in ('requirement','term_definition') and rid not in semantic_cards:continue
                if kind=='profile' and rid not in semantic_profiles:continue
                if kind=='fragment' and parse_refs.get(origin[0]) is not None:continue
                if kind=='structured_fragment' and value.get('parse_ref')!=parse_refs.get(origin[0]):continue
            elif kind in ('term_definition','structured_fragment'):continue
            refs.add((rid,latest_version))
            if kind=='requirement':requirements.add((rid,latest_version))
    if not requirements:raise NotReady('No extracted requirements to publish')
    if payload.get('curation'):
        from .publication import materialize
        originals={rid for rid,version in refs if by_id[rid][-1][1] in ('requirement','term_definition')}
        curated,policy=materialize(store,payload,originals,analyses=analyses if screened else None)
        refs.update(curated)
    for rid,versions in by_id.items():
        latest_version,kind,value=versions[-1]
        parent=value.get('requirement_ref') or value.get('from_ref')
        if kind in ('obligation','applicability','dependency') and parent and tuple(parent) in requirements:
            refs.add((rid,latest_version))
    # Keep exact transitive context; never infer that omitted source text was checked.
    dependencies=[]
    with store.connection() as db:
        pending=list(refs)
        while pending:
            rid,version=pending.pop()
            for link in db.execute('SELECT target_id,target_version FROM record_links WHERE record_id=? AND record_version=?',(rid,version)):
                target=tuple(link)
                if target not in refs:refs.add(target);pending.append(target)
        for rid,version in refs:
            row=db.execute('SELECT payload FROM records WHERE id=? AND version=? AND kind=\'dependency\'',(rid,version)).fetchone()
            if row:
                dependency=json.loads(row[0])
                if dependency.get('unresolved'):dependencies.append([rid,version])
    release_id=payload['release_id'];generation_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'generation:'+release_id))
    manifest=store.create_release(sid,release_id,generation_id,encoder.space.id,sorted(refs),payload['versions'])
    GenerationBuilder(store,encoder,vector).build(release_id)
    if not authorize(payload['actor_id'],sid,'upload'):raise PermissionError('Upload grant revoked')
    att=dict(canonical=True,fts=True,vector=True,provenance=True,manifest_hash=checksum(manifest),
             embedding_space=encoder.space.id,record_count=len(manifest['items']),watermark=1)
    result=store.attest_ready(release_id,att)
    result['limitations']={'unresolved_dependencies':len(dependencies)}
    if payload.get('curation'):
        from .publication import changes
        from .norm_runtime import projection_chunks
        rows=changes(store,release_id,payload.get('previous_release'))
        chunks=projection_chunks(rows)
        # Canonical artifact permits replay after a lost portal reply, without
        # rebuilding an index or substituting a newer expert selection.
        from .structure import atomic_json
        atomic_json(store.directory/'publication-material'/f'{release_id}.json',rows)
        result.update(material_count=len(rows),material_digest=checksum([checksum(c) for c in chunks]),
            curation_digest=checksum(payload['curation']),trust_summary={
                'total':len(policy['catalog']),
                'confirmed':sum(c['trust']['approval_current'] and not c['trust']['blocking_reasons'] for c in policy['catalog']),
                'blocked':sum(bool(c['trust']['blocking_reasons']) for c in policy['catalog']),
                'requires_reconfirmation':sum(c['trust']['requires_reconfirmation'] for c in policy['catalog'])})
        if screened:
            result['quality_summary']=policy['quality']
            result['trust_summary'].update(quality=policy['quality']['counts'])
    return store.remember_result(command_id,'release.prepare',payload,result)
