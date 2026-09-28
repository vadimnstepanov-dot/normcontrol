"""Versioned, restartable normative extraction; no release/index publication here."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import uuid
from .store import checksum
from .norms import extract, fragments_from_store, EXTRACTOR_VERSION


def projection_chunks(projection, *, max_bytes=750_000):
    """Bound each worker request by bytes as well as card count."""
    chunks=[];current=[];size=2
    for entry in projection:
        encoded=len(json.dumps(entry,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+1
        if encoded+2>max_bytes:raise ValueError('One normative card exceeds transfer limit')
        if current and (len(current)==100 or size+encoded>max_bytes):
            chunks.append(current);current=[];size=2
        current.append(entry);size+=encoded
    if current:chunks.append(current)
    return chunks


def analyze_source(store,set_id,source_id,*,client=None,cancel=lambda:False,parse_ref=None):
    source,blocks=fragments_from_store(store,set_id,source_id)
    if client is not None:
        return analyze_semantic_source(store,set_id,source_id,source,blocks,client,cancel,parse_ref)
    run_id=checksum([EXTRACTOR_VERSION,source_id,source['sha256'],[(b['id'],b['context_hash']) for b in blocks]])
    folder=store.directory/'analyses';folder.mkdir(exist_ok=True)
    path=folder/(run_id+'.json')
    if path.exists():
        result=json.loads(path.read_text(encoding='utf-8'))
        result['summary']['candidate_digest']=checksum([checksum(chunk) for chunk in projection_chunks(result['projection'])])
        (folder/(str(source_id)+'.complete')).write_text(run_id,encoding='ascii')
        return result
    result=extract(source_id,source,blocks);fragment_map={b['locator']:b for b in blocks};entries=[]
    for profile in result['profiles']:
        entries.append((set_id,'profile',profile['id'],1,dict(definition=profile,source_revision=[source_id,1])))
    for card in result['cards']:
        refs=list(dict.fromkeys((fragment_map[c['locator']]['id'],1) for c in card['citations']))
        payload=dict(fragment_refs=[list(x) for x in refs],modality=card['modality'],condition=card['applicability'],
                     source_revision=[source_id,1],extractor_version=EXTRACTOR_VERSION,card=card)
        entries.append((set_id,'requirement',card['id'],1,payload))
        for i,atom in enumerate(card['obligations']):
            oid=str(uuid.uuid5(uuid.UUID(card['id']),'obligation:'+str(i)))
            entries.append((set_id,'obligation',oid,1,dict(requirement_ref=[card['id'],1],**atom)))
        for i,dep in enumerate(card['dependencies']):
            did=str(uuid.uuid5(uuid.UUID(card['id']),'dependency:'+str(i)))
            target=[fragment_map[dep['target']]['id'],1] if not dep['unresolved'] else None
            entries.append((set_id,'dependency',did,1,dict(from_ref=[card['id'],1],relation=dep['relation'],
                           target_ref=target,unresolved=dep['unresolved'],required=dep['required'],pointer=dep.get('pointer',dep['target']))))
        aid=str(uuid.uuid5(uuid.UUID(card['id']),'applicability:unbound'))
        entries.append((set_id,'applicability',aid,1,dict(requirement_ref=[card['id'],1],result='unknown',
                       criteria=card['applicability'],evidence=[],reason='Project facts not bound at extraction stage')))
    store.put_records_batch(entries)
    projection=[]
    for card in result['cards']:
        projection.append({k:card[k] for k in ('id','locator','state','modality','profile_id','scope','category','validation',
            'citations','obligations','conditions','exceptions','dependencies','applicability')})
    # Non-extracted fragments stay in the curator queue too; no candidate is not a positive decision.
    review_queue=[dict(locator=c['locator'],candidate_id=c['id'],reasons=c['validation']['completeness']['reasons'])
                  for c in result['cards'] if c['state']=='needs_review']
    review_queue.extend(dict(locator=r['locator'],candidate_id=None,reasons=[r['reason']])
                        for r in result['coverage'] if r['state']=='needs_review')
    ingestion_path=store.directory/'coverage'/(source_id+'.json')
    ingestion=json.loads(ingestion_path.read_text(encoding='utf-8')) if ingestion_path.exists() else {}
    unreadable=[x for x in ingestion.get('coverage',[]) if x['state']=='unreadable']
    summary=dict(kind='source.analyzed',set_id=set_id,source_id=source_id,run_id=run_id,extractor_version=EXTRACTOR_VERSION,
        source_sha256=source['sha256'],fragments=len(blocks),candidate_count=len(projection),
        obligation_count=sum(len(c['obligations']) for c in result['cards']),
        provenance=dict(Counter(c['validation']['provenance']['status'] for c in result['cards'])),
        states=dict(Counter(c['state'] for c in result['cards'])),modalities=dict(Counter(c['modality'] for c in result['cards'])),
        scopes=dict(Counter(c['scope'] for c in result['cards'])),categories=dict(Counter(c['category'] for c in result['cards'])),
        unresolved_dependencies=sum(d['unresolved'] for c in result['cards'] for d in c['dependencies']),
        semantic_completeness='unknown' if review_queue or unreadable or result['coverage_audit']['gaps'] else 'verified_simple',
        scope='one_source_all_extracted_fragments',coverage_audit=result['coverage_audit'],unreadable_count=len(unreadable),
        review_queue_count=len(review_queue),profiles=result['profiles'],violation_count=None,
        candidate_digest=checksum([checksum(chunk) for chunk in projection_chunks(projection)]))
    result.update(run_id=run_id,summary=summary,projection=projection,review_queue=review_queue,unreadable=unreadable)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,prefix='.analysis-',delete=False) as f:
        temporary=Path(f.name);json.dump(result,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)
    marker=folder/(str(source_id)+'.complete')
    marker.write_text(run_id,encoding='ascii')
    return result


def analyze_semantic_source(store,set_id,source_id,source,blocks,client,cancel,parse_ref):
    from .semantic import extract_semantic, VERSION
    from .structure import atomic_json
    source_gaps=[]
    if parse_ref is not None:
        with store.connection() as db:
            row=db.execute("SELECT payload FROM records WHERE id=? AND version=? AND set_id=? AND kind='parse_run'",(*parse_ref,set_id)).fetchone()
            if not row or json.loads(row[0])['source_revision']!=[source_id,1]:raise ValueError('Parse/source mismatch')
            parse=json.loads(row[0]);artifact=(store.directory/parse['artifact_key']).resolve()
            if not artifact.is_relative_to(store.directory):raise ValueError('Unsafe parse artifact')
            material=json.loads(artifact.read_text(encoding='utf8'))
            from .store import checksum as digest
            verified=dict(material);declared=verified.pop('result_digest')
            if declared!=parse['manifest_digest'] or digest(verified)!=declared:raise ValueError('Parse artifact digest mismatch')
            source_gaps=material.get('coverage',[])
            blocks=[]
            for row in db.execute("SELECT id,payload FROM records WHERE set_id=? AND kind='structured_fragment' ORDER BY rowid",(set_id,)):
                b=json.loads(row['payload'])
                if b['parse_ref']==parse_ref:
                    blocks.append(dict(b['structure'],id=row['id'],locator=b['locator'],exact_text=b['exact_text'],
                        context_hash=b['context_hash'],source_sha256=source['sha256']))
    if not blocks:raise ValueError('No material to analyze')
    result=extract_semantic(source_id,source,blocks,store.directory,client,cancel=cancel,parse_ref=parse_ref)
    result['source_gaps']=source_gaps
    if source_gaps:result['complete']=False
    entries=[];fragment_map={b['locator']:b for b in blocks}
    for profile in result['profiles']:
        entries.append((set_id,'profile',profile['id'],1,dict(definition=profile,source_revision=[source_id,1])))
    for card in result['cards']:
        refs=list(dict.fromkeys((fragment_map[c['locator']]['id'],1) for c in card['citations']))
        kind='term_definition' if card['entity_type']=='definition' else 'requirement'
        payload=dict(source_revision=[source_id,1],fragment_refs=[list(r) for r in refs],modality=card['modality'],
            condition=card['applicability'],extractor_version=VERSION,analysis_id=result['run_id'],card=card)
        entries.append((set_id,kind,card['id'],1,payload))
        for index,atom in enumerate(card['obligations']):
            entries.append((set_id,'obligation',str(uuid.uuid5(uuid.UUID(card['id']),'obligation:'+str(index))),1,
                            dict(requirement_ref=[card['id'],1],**atom)))
        for index,dep in enumerate(card['dependencies']):
            entries.append((set_id,'dependency',str(uuid.uuid5(uuid.UUID(card['id']),'dependency:'+str(index))),1,
                            dict(from_ref=[card['id'],1],target_ref=None,**dep)))
    aid=str(uuid.uuid5(uuid.UUID(source_id),'analysis:'+result['run_id']))
    entries.append((set_id,'analysis_run',aid,1,dict(source_revision=[source_id,1],run_id=result['run_id'],
        extractor_version=VERSION,coverage_audit=result['coverage_audit'],parse_ref=parse_ref)))
    if cancel():raise InterruptedError('Analysis lease lost before canonical commit')
    store.put_records_batch(entries)
    projection=result['cards']
    summary=dict(kind='source.analyzed',set_id=set_id,source_id=source_id,run_id=result['run_id'],extractor_version=VERSION,
        source_sha256=source['sha256'],fragments=len(blocks),candidate_count=len(projection),
        obligation_count=sum(len(c['obligations']) for c in projection),
        provenance=dict(Counter(c['validation']['provenance']['status'] for c in projection)),
        states=dict(Counter(c['state'] for c in projection)),modalities=dict(Counter(c['modality'] for c in projection)),
        scopes=dict(Counter(c['scope'] for c in projection)),categories=dict(Counter(c['category'] for c in projection)),
        unresolved_dependencies=sum(len(c['dependencies']) for c in projection),
        semantic_completeness='model_reviewed' if result['complete'] else 'partial',
        coverage_audit=result['coverage_audit'],profiles=result['profiles'],violation_count=None,
        glossary_count=len(result['glossary']),metrics=result['metrics'],analysis_errors=len(result['errors']),
        uncertain_card_count=sum(bool(c['ambiguities']) for c in projection),
        rejected_proposal_count=len(result.get('rejected_proposals',[])),
        source_gap_count=len(source_gaps),
        candidate_digest=checksum([checksum(chunk) for chunk in projection_chunks(projection)]))
    result.update(summary=summary,projection=projection,review_queue=result['coverage_audit']['gaps'])
    folder=store.directory/'analyses';folder.mkdir(exist_ok=True)
    atomic_json(folder/(result['run_id']+'.json'),result)
    # A partial new analysis cannot silently replace the last complete release input.
    if result['complete']:(folder/(source_id+'.complete')).write_text(result['run_id'],encoding='ascii')
    return result
