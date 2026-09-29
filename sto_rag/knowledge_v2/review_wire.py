"""Lossless semantic transport; canonical provenance remains in the task journal."""
import json
from .store import checksum,encode

VERSION='normative-wire-v5'
POLICY='''Представление transport использует справочники без сокращения текста.
documents — строки с колонками document_columns; id строки является block_id для
цитаты. structure_ref раскрывается в document_structures: там документ, заголовки,
параметры таблицы и пути заголовков ячеек. context_refs требований раскрываются в
normative_contexts, normative structure_ref — в normative_structures. Все условия,
исключения и зависимости необходимо учитывать совместно с точным текстом нормы.
В completeness gaps с reason_ref раскрываются по gap_reasons. Неизвестность и
непрочитанные области сохраняются; это не разрешение делать глобальные выводы.
row в строке documents — номер строки таблицы; header_ref раскрывается в
document_headers и сохраняет путь заголовков ячейки. applicability_ref раскрывается
в applicability_contexts. source_ref обозначает неизменный первоисточник из sources.
Нормативный exact_text сохранён дословно; поисковые копии и контрольные хеши не нужны
для решения. Отсутствие служебного хеша не означает отсутствие условия или нормы.
Указывай только id требований и id строк documents; справочники не являются
доказательствами исполнения. Кратко объясняй решение, избегая повтора нормы.'''

class Identities(dict):
    def __init__(self,*args,blocks=None,**kwargs):
        super().__init__(*args,**kwargs);self.blocks=blocks or {}

class Pool:
    def __init__(self,prefix):self.prefix=prefix;self.items={};self.keys={}
    def add(self,value):
        key=checksum(value)
        if key not in self.keys:
            alias=self.prefix+str(len(self.items)+1);self.keys[key]=alias;self.items[alias]=value
        return self.keys[key]

def payload(original):
    rows=original.get('obligations',[])
    if not rows:return original,Identities()
    value=json.loads(encode(original));ids=[r['id'] for r in rows]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate input obligation')
    aliases={rid:f'R{i+1:03d}' for i,rid in enumerate(ids)}
    # Scope and source/target identities stay consistent across all packet fields.
    document_ids=list(dict.fromkeys([b['document'] for b in value.get('documents',[]) if b.get('document')]+
        value.get('completeness',{}).get('document_ids',[])))
    documents={rid:f'D{i+1}' for i,rid in enumerate(document_ids)}
    blocks={b['id']:f'B{i+1:03d}' for i,b in enumerate(value.get('documents',[]))}
    if len(blocks)!=len(value.get('documents',[])):raise ValueError('Duplicate document block')
    doc_structures=Pool('S');norm_structures=Pool('N');contexts=Pool('C')
    headers=Pool('H');sources=Pool('F');applicability=Pool('A')
    packed=[]
    for block in value.get('documents',[]):
        structure={k:v for k,v in block.items() if k not in ('id','text','locator','location','row','header_path') and v not in (None,[],{})}
        if 'document' in structure:structure['document']=documents[structure['document']]
        packed.append([blocks[block['id']],block['text'],doc_structures.add(structure),block.get('locator',''),
                       block.get('row'),headers.add(block['header_path']) if block.get('header_path') else None])
    # Fields describing human approval, disk locations and record hashes are
    # enforced by the runner; they are not instructions or evidence for the LLM.
    administrative={'profile_versions','execution_issues','issues','publication_trust','curation_ref','lineage',
        'source','source_revision','version','release_id','generation_id','requirement_ref','obligation_id'}
    for row in value['obligations']:
        row['id']=aliases[row['id']]
        for key in administrative:row.pop(key,None)
        atom=row.get('atom',{})
        if isinstance(atom,dict):
            for key in ('id','requirement_ref'):atom.pop(key,None)
            # These are parser fingerprints and offsets, never normative text.
            # Keep quotes, routes, conditions, exceptions and unknown fields intact.
            for citation in atom.get('citations',[]):
                for key in ('context_hash','start','end'):citation.pop(key,None)
                if 'source_sha256' in citation:citation['source_ref']=sources.add(citation.pop('source_sha256'))
        if isinstance(row.get('applicability'),dict):
            fact=row.pop('applicability');fact.pop('facts_hash',None)
            row['applicability_ref']=applicability.add(fact)
        if row.get('document_id') in documents:row['document_id']=documents[row['document_id']]
        for key in ('source_documents','target_documents'):
            if key in row:row[key]=[documents.get(d,d) for d in row[key]]
        if isinstance(row.get('context'),list) and all(isinstance(f,dict) for f in row['context']):
            refs=[]
            for fragment in row.pop('context'):
                fragment={k:v for k,v in fragment.items() if k not in ('context_hash','parse_ref','search_text')}
                # Retain source identity (including revision) through a compact alias.
                for key in ('ref','source_revision'):
                    if key in fragment:fragment[key+'_alias']=sources.add(fragment.pop(key))
                structure=fragment.pop('structure',None)
                if isinstance(structure,dict):
                    structure={k:v for k,v in structure.items() if k not in ('search_text','source_sha256','source_locator','word_render_pdf','numbering_evidence','numbering_method')}
                    fragment['structure_ref']=norm_structures.add(structure)
                refs.append(contexts.add(fragment))
            row['context_refs']=refs
    scope=value.get('completeness',{})
    for key in ('expected_ids','submitted_ids'):
        if key in scope:
            identities=scope.pop(key);stem=key.removesuffix('_ids')
            scope[stem+'_block_count']=len(identities);scope[stem+'_ids_digest']=checksum(identities)
    if 'document_ids' in scope:scope['document_ids']=[documents.get(d,d) for d in scope['document_ids']]
    reasons=Pool('G')
    if isinstance(scope.get('gaps'),list):
        for gap in scope['gaps']:
            if 'reason' in gap:gap['reason_ref']=reasons.add(gap.pop('reason'))
        if reasons.items:scope['gap_reasons']=reasons.items
    for decision in value.get('proposed',[]):
        if decision.get('obligation_id') not in aliases:raise ValueError('Proposed decision outside request')
        decision['obligation_id']=aliases[decision['obligation_id']]
        for evidence in decision.get('evidence',[]):
            if evidence.get('block_id') not in blocks:raise ValueError('Proposed evidence outside request')
            evidence['block_id']=blocks[evidence['block_id']]
            for key in ('document','locator','location'):evidence.pop(key,None)
    # Stable normative/document prefix lets check/verify share the same input.
    # Unlike canonical hashing, transport serialization preserves this order.
    result=dict(transport=VERSION,normative_structures=norm_structures.items,normative_contexts=contexts.items,
        obligations=value['obligations'],document_structures=doc_structures.items,document_headers=headers.items,
        applicability_contexts=applicability.items,sources=sources.items,
        document_columns=['id','text','structure_ref','locator','row','header_ref'],documents=packed)
    result.update({k:v for k,v in value.items() if k not in ('obligations','documents','stage','proposed')})
    if 'stage' in value:result['stage']=value['stage']
    if 'proposed' in value:result['proposed']=value['proposed']
    return result,Identities({alias:rid for rid,alias in aliases.items()},blocks={alias:rid for rid,alias in blocks.items()})

def serialize(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)

def restore(result,identities):
    for decision in result.get('decisions',[]):
        if not isinstance(decision,dict) or decision.get('obligation_id') not in identities:raise ValueError('Decision alias outside request')
        decision['obligation_id']=identities[decision['obligation_id']]
        for evidence in decision.get('evidence',[]):
            if evidence.get('block_id') not in identities.blocks:raise ValueError('Evidence alias outside request')
            evidence['block_id']=identities.blocks[evidence['block_id']]
    return result
