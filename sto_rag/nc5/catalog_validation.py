"""Reconstruct each card from its source ledger. Semantic judgments remain separate."""
from collections import Counter,defaultdict
from pathlib import Path
from .common import DATA,ROOT,read,write,digest
from .catalog import load_catalog,INCLUDED_KINDS

def audit(version=None):
    cat=load_catalog(version);errors=[];sources={s['source_id']:s for s in cat['sources']};cards={c['requirement_id']:c for c in cat['cards']};by_source=defaultdict(list);fragments={}
    for entry in cat['ledger']:by_source[entry['source_id']].append(entry)
    for sid,source in sources.items():
        path=DATA/source['snapshot'] if source.get('snapshot') else ROOT/source['file']
        if not path.exists() or digest(path.read_bytes())!=source['sha256']:errors.append({'source':sid,'error':'source_hash_mismatch'})
        extracted=path.parent/'extracted.json'
        if extracted.exists():
            blocks=read(extracted)['blocks'];text=[b.get('raw_text',b['text']) for b in blocks]
        else:
            extracted=path.parent/'extracted.txt'
            if not extracted.exists():errors.append({'source':sid,'error':'extraction_missing'});continue
            text=[s.strip() for s in extracted.read_text(encoding='utf-8-sig').splitlines() if s.strip()]
        fragments[sid]=text
        indices=[e['index'] for e in by_source[sid]]
        if Counter(indices)!=Counter(range(len(text))):errors.append({'source':sid,'error':'ledger_coverage_mismatch','actual_fragments':len(text),'ledger_entries':len(indices)})
    checked=[]
    for rid,c in cards.items():
        indices=sorted(e['index'] for e in by_source[c['source_id']] if rid in e['requirement_ids'])
        try:quote='\n'.join(fragments[c['source_id']][i] for i in indices)
        except (KeyError,IndexError):quote=None
        if quote!=c['source_quote']:errors.append({'requirement_id':rid,'error':'source_quote_mismatch'})
        else:checked.append(rid)
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
    folder=DATA/'catalogs'/cat['version'];write(folder/'validation.json',result);write(folder/'profile-matrix.json',matrix)
    return result

if __name__=='__main__':
    from .common import dumps
    print(dumps(audit()))
