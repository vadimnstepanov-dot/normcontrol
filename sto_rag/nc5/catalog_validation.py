"""Reconstruct each card from its source ledger. Semantic judgments remain separate."""
from collections import Counter,defaultdict
from pathlib import Path
from .common import DATA,ROOT,read,write,digest
from .catalog import load_catalog,INCLUDED_KINDS

def audit(version=None):
    cat=load_catalog(version);errors=[];sources={s['source_id']:s for s in cat['sources']};cards={c['requirement_id']:c for c in cat['cards']};by_source=defaultdict(list);fragments={};located={}
    for entry in cat['ledger']:by_source[entry['source_id']].append(entry)
    for sid,source in sources.items():
        path=DATA/source['snapshot'] if source.get('snapshot') else ROOT/source['file']
        if not path.exists() or digest(path.read_bytes())!=source['sha256']:errors.append({'source':sid,'error':'source_hash_mismatch'})
        extracted=path.parent/'extracted.json'
        if extracted.exists():
            blocks=read(extracted)['blocks'];text=[b.get('raw_text',b['text']) for b in blocks]
            located[sid]={(b['locator'],b.get('raw_text',b['text'])) for b in blocks}
        else:
            extracted=path.parent/'extracted.txt'
            if not extracted.exists():errors.append({'source':sid,'error':'extraction_missing'});continue
            text=[s.strip() for s in extracted.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
            located[sid]={(f'текстовый абзац {i+1}',t) for i,t in enumerate(text)}
        fragments[sid]=text
        indices=[e['index'] for e in by_source[sid]]
        if Counter(indices)!=Counter(range(len(text))):errors.append({'source':sid,'error':'ledger_coverage_mismatch','actual_fragments':len(text),'ledger_entries':len(indices)})
    checked=[]
    for rid,c in cards.items():
        indices=sorted(e['index'] for e in by_source[c['source_id']] if rid in e['requirement_ids'])
        try:quote='\n'.join(fragments[c['source_id']][i] for i in indices)
        except (KeyError,IndexError):quote=None
        exact=quote==c['source_quote']
        if c.get('parent_requirement_id'):
            offset=c.get('source_char_offset',-1)
            exact=isinstance(offset,int) and offset>=0 and quote==c.get('source_parent_quote') and quote[offset:offset+len(c['source_quote'])]==c['source_quote']
        if not exact:errors.append({'requirement_id':rid,'error':'source_quote_mismatch'})
        else:checked.append(rid)
        for dep in c.get('normative_dependencies',[]):
            if (dep.get('locator'),dep.get('quote')) not in located.get(dep.get('source_id'),set()):errors.append({'requirement_id':rid,'error':'dependency_quote_mismatch','locator':dep.get('locator')})
        for obligation in c.get('obligations',[]):
            if obligation.get('quote','') not in c['source_quote']:errors.append({'requirement_id':rid,'error':'obligation_quote_mismatch'})
    for e in cat['ledger']:
        if e['kind'] in INCLUDED_KINDS and not e['requirement_ids'] and e.get('reason')=='Контекст; не самостоятельное предписание':errors.append({'source':e['source_id'],'index':e['index'],'error':'normative_fragment_dropped'})
        for rid in e['requirement_ids']:
            if rid not in cards:errors.append({'source':e['source_id'],'index':e['index'],'error':'unknown_card'})
        if not e['requirement_ids'] and not e.get('reason'):errors.append({'source':e['source_id'],'index':e['index'],'error':'unexplained_exclusion'})
    matrix=[]
    for p in cat['profiles']:
        rules=[]
        for c in cat['cards']:
            included=c['document_types']==['general'] or p['id'] in c['document_types']
            rules.append({'requirement_id':c['requirement_id'],'included':included,'stage':c['check_stage'],'reason':'Общее положение; условия применимости проверяются по источнику' if included and c['document_types']==['general'] else 'Шаблон данного типа' if included else 'Другой шаблон или опциональная инструкция','applicability':c['applicability'] if included else None})
        matrix.append({'profile':p,'rules':rules})
    result={'catalog':cat['version'],'source_cards_checked':len(checked),'fragment_count':sum(len(x) for x in fragments.values()),'errors':errors,'exact_source_validation':'passed' if not errors else 'failed','semantic_validation':'pending','semantic_warning':'Точное происхождение не доказывает полноту смыслового выделения обязанностей или применимость.'}
    result['validation_dimensions']={
        'provenance':{'status':'failed' if errors else 'passed','cards_checked':len(checked)},
        'obligation_completeness':{'status':'requires_semantic_review','cards_with_obligations':sum(bool(c.get('obligations')) for c in cards.values()),'excluded_fragments':sum(not x['requirement_ids'] for x in cat['ledger'])},
        'applicability':{'status':'requires_project_evidence','annotated_cards':sum(bool(c.get('applicability_contract')) for c in cards.values())},
        'dependencies':{'status':'unresolved' if any(c.get('unresolved_dependencies') for c in cards.values()) else 'resolved','cards_with_unresolved_references':sum(bool(c.get('unresolved_dependencies')) for c in cards.values())}}
    from .normative_contract import NORM
    review=[];outside_scope=[]
    for entry in cat['ledger']:
        if entry['kind'] in ('метаданные','пример'):continue
        text=fragments.get(entry['source_id'],[])[entry['index']]
        if not NORM.search(text):continue
        linked=[cards[rid] for rid in entry['requirement_ids'] if rid in cards]
        intervals=[]
        for c in linked:
            if text in c['source_quote']:intervals.append((0,len(text)))
            for part in c['source_quote'].splitlines():
                start=text.find(part.strip())
                if part.strip() and start>=0:intervals.append((start,start+len(part.strip())))
        missing=[text[max(0,m.start()-70):min(len(text),m.end()+220)] for m in NORM.finditer(text) if not any(start<=m.start()<end for start,end in intervals)]
        if missing:
            candidate={'source_id':entry['source_id'],'locator':entry['locator'],'kind':entry['kind'],'uncovered_candidates':missing,'reason':'Requires semantic review; not automatically a lost obligation'}
            if not entry['requirement_ids'] and entry.get('reason','').startswith('Делопроизводство/контекст'):
                candidate['reason']='Outside the configured instruction chapters; applicability must be expanded explicitly'
                outside_scope.append(candidate)
            else:review.append(candidate)
    result['validation_dimensions']['obligation_completeness']['review_candidates']=len(review)
    result['obligation_review_candidates']=review
    result['validation_dimensions']['obligation_completeness']['outside_instruction_scope']=len(outside_scope)
    result['instruction_scope_exclusions']=outside_scope
    folder=DATA/'catalogs'/cat['version'];write(folder/'validation.json',result);write(folder/'profile-matrix.json',matrix)
    return result

if __name__=='__main__':
    from .common import dumps
    print(dumps(audit()))
