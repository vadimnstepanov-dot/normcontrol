"""Source-neutral normative candidate extraction with explicit uncertainty."""
import re
import uuid
from .store import checksum
from . import norm_validation as validators

EXTRACTOR_VERSION='norms-v1.1'
MODALS=[('prohibited',r'не\s+(?:допускается|долж(?:ен|на|но|ны))|запрещ(?:ено|ается)'),
        ('recommended',r'рекоменду(?:ется|ются)|рекомендовано'),
        ('permitted',r'допускается|разрешается'),
        ('mandatory',r'долж(?:ен|на|но|ны)|необходимо|следует|требуется|обязательно')]
MODAL_RE=re.compile('|'.join('(?P<'+name+'>'+pattern+')' for name,pattern in MODALS),re.I)
IMPLICIT=re.compile(r'\b(?:привод(?:ится|ятся)|описыва(?:ется|ются)|указыва(?:ется|ются)|формулируется|'
                    r'отража(?:ется|ются)|содержит|содержат|включает|включают|описывают|приводят|указывают|оформляют)\b',re.I)
CONDITION=re.compile(r'\b(?:если|в случае|при необходимости|при наличии|при отсутствии|при условии)\b',re.I)
EXCEPTION=re.compile(r'\b(?:за исключением|кроме|исключая|если иное)\b',re.I)
REFERENCE=re.compile(r'\b(?:(?:пункт(?:а|е|ов|у)?|раздел(?:а|е)?|таблиц[аеуы]|рисунк[аеу])\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)|((?:ГОСТ|СТО|ISO)\s+[^,;\n]{1,55}))',re.I)


def citation(fragment,start=0,end=None):
    end=len(fragment['exact_text']) if end is None else end
    return dict(locator=fragment['locator'],start=start,end=end,quote=fragment['exact_text'][start:end],
                context_hash=fragment['context_hash'],source_sha256=fragment['source_sha256'])


def fragments_from_store(store,set_id,source_id):
    import json
    with store.connection() as db:
        source=db.execute("SELECT payload FROM records WHERE id=? AND version=1 AND set_id=? AND kind='source_revision'",(source_id,set_id)).fetchone()
        if not source:raise ValueError('Source not in selected set')
        source=json.loads(source[0]);blocks=[]
        for row in db.execute("SELECT id,payload FROM records WHERE set_id=? AND kind='fragment' ORDER BY rowid",(set_id,)):
            value=json.loads(row['payload'])
            if value['source_revision']!=[source_id,1]:continue
            blocks.append(dict(**value['structure'],locator=value['locator'],exact_text=value['exact_text'],
                               search_text=value['search_text'],context_hash=value['context_hash'],id=row['id'],
                               source_sha256=source['sha256']))
    return source,blocks


def sentence_spans(text):
    # Keep decimal clause numbers intact; split only actual punctuation plus whitespace.
    start=0
    for match in re.finditer(r'(?<=[.;!?])\s+(?=[А-ЯA-Z\-–—])',text):
        if text[start:match.start()].strip():yield start,match.start()
        start=match.end()
    if text[start:].strip():yield start,len(text)


def extract(source_id,source,blocks):
    fragments={b['locator']:b for b in blocks};cards=[];ledger=[];profiles=[];current_profile=None;parent=None;example=False
    by_clause={};by_table={}
    for b in blocks:
        if b.get('clause'):by_clause.setdefault(b['clause'],[]).append(b['locator'])
        if b.get('table') is not None:by_table.setdefault(b['table'],[]).append(b)
    common_id='profile-'+checksum([source_id,'common'])[:20]
    profile_by_heading={}
    # Source-wide explanatory policies remain cited context for every appendix.
    policies=[b for b in blocks if re.search(r'шаблон',b['exact_text'],re.I)
              and re.search(r'пример|приоритет',b['exact_text'],re.I) and not b.get('is_heading')
              and b.get('kind')=='paragraph' and len(b.get('heading_refs',[]))<=1]
    def category(block):
        path=' '.join(block.get('heading_path',[])).casefold()
        if re.search(r'перечень.*(?:виды документов|жизненн)|состав.*комплект|обязательност',path):return 'document_set'
        if re.search(r'оформлен|нумераци|титульн|лист подпис|сокращен|децимальн',path):return 'formatting'
        return 'content'
    for block in blocks:
        text=block['exact_text'];loc=block['locator']
        definition=bool(re.search(r'термины и определения', ' '.join(block.get('heading_path',[])),re.I))
        title=re.search(r'Шаблон\s+документ(?:а|ов)?\s+[«"]([^»"]+)',text,re.I) if block.get('is_heading') else None
        if title:
            name=title[1];pid='profile-'+checksum([source_id,name])[:20]
            current_profile=dict(id=pid,name=name,version=1,basis=[citation(block)],
                kind='document_type',inherits=[common_id],
                expression={'all_of':[{'fact':{'name':'selected_sources','in':[source_id]}},
                                      {'fact':{'name':'document_type','in':[name]}}]})
            profiles.append(current_profile);profile_by_heading[loc]=current_profile
        if block.get('is_heading'):
            parent=None;example=False
            if not title and not any(h in profile_by_heading for h in block.get('heading_refs',[])):
                current_profile=None
        if re.match(r'^\s*Пример(?:ы)?\s*[–—\-:]?',text,re.I):example=True
        is_list=bool(re.match(r'^\s*(?:[-–—•]|\d+[)\.]\s)',text) or block.get('numbering',{}).get('num') not in (None,'0'))
        if example and not (re.match(r'^\s*Пример',text,re.I) or is_list):example=False
        if parent and not is_list:parent=None
        spans=list(sentence_spans(text));atoms=[];modalities=[];explicit=False;conditions=[];exceptions=[];dependencies=[];ambiguities=[]
        inherited_modality=parent['modality'] if parent else None
        for a,z in spans:
            part=text[a:z];matches=list(MODAL_RE.finditer(part))
            merged=[]
            for match in matches:
                if merged and match.lastgroup==merged[-1].lastgroup and not part[merged[-1].end():match.start()].strip():
                    continue
                merged.append(match)
            matches=merged
            if matches:
                for i,m in enumerate(matches):
                    begin=a if i==0 else a+m.start();end=a+matches[i+1].start() if i+1<len(matches) else z
                    # Include each modal scope separately; fields always point back to a quote.
                    quote=citation(block,begin,end);subject=part[:m.start()].strip(' ,:') or 'document_author'
                    atoms.append(dict(subject=subject,action=m.group(),object=text[a+m.end():end].strip() or part,
                                      citation=quote,modality=m.lastgroup))
                    modalities.append(m.lastgroup);explicit=True
            else:
                m=IMPLICIT.search(part)
                if m and current_profile:
                    atoms.append(dict(subject=part[:m.start()].strip(' ,:') or 'document_author',action=m.group(),
                                      object=part[m.end():].strip() or part,citation=citation(block,a,z),modality='mandatory'))
                    modalities.append('mandatory');ambiguities.append('implicit_template_obligation')
        if not atoms and parent and is_list and text.strip():
            atoms.append(dict(subject='document_author',action='include_item',object=text,citation=citation(block),
                              modality=inherited_modality));modalities.append(inherited_modality)
        if not atoms and block.get('table') is not None and re.fullmatch(r'\s*(?:Да(?:/нет)?|Нет)\s*(?:\d+\))?\s*',text,re.I):
            atoms.append(dict(subject='document_set',action='interpret_matrix_cell',object=text,
                              citation=citation(block),modality='unknown'))
            modalities.append('unknown');ambiguities.append('matrix_requires_legend_stage_work_type_and_exceptions')
            conditions.append({'kind':'unknown','text':'Table legend and project stage/work type required','citation':citation(block)})
        if parent and is_list:
            dependencies.append(dict(relation='parent_condition',target=parent['locator'],required=True,unresolved=False))
        if CONDITION.search(text):conditions.append({'kind':'unknown','text':text,'citation':citation(block)})
        if EXCEPTION.search(text):exceptions.append({'kind':'unknown','text':text,'citation':citation(block)})
        if parent:
            conditions+=parent['conditions'];exceptions+=parent['exceptions']
        for href in block.get('heading_refs',[]):
            if href!=loc:
                dependencies.append(dict(relation='heading',target=href,required=True,unresolved=href not in fragments))
                if href in fragments and CONDITION.search(fragments[href]['exact_text']):
                    conditions.append({'kind':'unknown','text':fragments[href]['exact_text'],'citation':citation(fragments[href])})
        if block.get('table') is not None:
            for other in by_table[block['table']]:
                if other['locator']!=loc and (other.get('row')==block.get('row') or other.get('row',99)<=3
                                               or other.get('locator') in block.get('note_refs',[])):
                    dependencies.append(dict(relation='table_context',target=other['locator'],required=True,unresolved=False))
            for other in blocks:
                if (other.get('kind')=='paragraph' and other.get('heading_refs')==block.get('heading_refs')
                    and re.match(r'^Примечани',other['exact_text'],re.I)):
                    dependencies.append(dict(relation='note',target=other['locator'],required=True,unresolved=False))
                    conditions.append({'kind':'unknown','text':other['exact_text'],'citation':citation(other)})
        if current_profile:
            for policy in policies:
                dependencies.append(dict(relation='source_policy',target=policy['locator'],required=True,unresolved=False))
        for ref in REFERENCE.finditer(text):
            options=by_clause.get(ref.group(1),[]) if ref.group(1) else []
            # Repeated numbering in appendices is not a resolvable global pointer.
            options=[candidate for candidate in options if fragments[candidate].get('heading_path',[])[:1]==block.get('heading_path',[])[:1]]
            dependencies.append(dict(relation='cross_reference',target=options[0] if len(options)==1 else None,
                                     pointer=ref.group(),required=True,unresolved=len(options)!=1))
        modality=modalities[0] if len(set(modalities))==1 else 'mixed'
        if title:atoms=[]
        if atoms and text.strip():
            # A closed, explicitly quoted field enumeration can be split without guessing its grammar.
            # Preserve the entire modal scope as evidence for every child field.
            expanded=[]
            for atom in atoms:
                fields=list(re.finditer(r'«([^»]+)»',atom['object']))
                if len(fields)>1 and re.search(r'(?:столбц|пол[яе]|параметр).*:',atom['object'],re.I):
                    for field in fields:
                        expanded.append(dict(atom,action='include_field',object=field[1],group='required_fields'))
                else:expanded.append(atom)
            atoms=expanded
            cites=[]
            for atom in atoms:
                if atom['citation'] not in cites:cites.append(atom['citation'])
            for dep in dependencies:
                if not dep['unresolved'] and dep['target'] in fragments and fragments[dep['target']]['exact_text']:
                    c=citation(fragments[dep['target']])
                    if c not in cites:cites.append(c)
            if parent:
                c=citation(fragments[parent['locator']])
                if c not in cites:cites.append(c)
            if conditions or exceptions:
                c=citation(block)
                if c not in cites:cites.append(c)
            expression=current_profile['expression'] if current_profile else {'fact':{'name':'selected_sources','in':[source_id]}}
            if conditions or exceptions:
                expression={'all_of':[expression,{'unknown':'Unresolved source condition/exception'}]}
            logical='any_of' if re.search(r'\bлибо\b|\bили\b',text,re.I) else 'all_of'
            if logical=='any_of':ambiguities.append('alternative_requires_scope_validation')
            if re.search(r'\bи\b',text,re.I) and not all(a.get('group')=='required_fields' for a in atoms):
                ambiguities.append('coordination_requires_atomization')
            card=dict(id=str(uuid.uuid5(uuid.UUID(source_id),EXTRACTOR_VERSION+':'+loc+':'+block['context_hash'])),
                      locator=loc,fragment_id=block['id'],modality=modality,explicit=explicit,example=example,definition=definition,
                      obligations=atoms,citations=cites,conditions=conditions,exceptions=exceptions,dependencies=dependencies,
                      composition={logical:list(range(len(atoms)))},operation='semantic',applicability=expression,
                      profile_id=current_profile['id'] if current_profile else None,ambiguities=sorted(set(ambiguities)))
            card.update(scope='document_type' if current_profile else 'common',category=category(block),
                        effective_profile_id=current_profile['id'] if current_profile else common_id)
            card['validation']={'provenance':validators.provenance(card,fragments),
                                'completeness':validators.completeness(card,fragments,dependencies),
                                'applicability':validators.applicability(card,{})}
            good=card['validation']['provenance']['status']=='verified' and card['validation']['completeness']['semantic']=='verified_simple'
            card['state']='example' if example else 'definition' if definition else 'validated' if good else 'needs_review'
            cards.append(card)
            ledger.append(dict(locator=loc,state='example' if example else 'definition' if definition else 'normative',candidate_id=card['id'],reason=card['state']))
            if text.rstrip().endswith(':'):
                parent={'locator':loc,'modality':modality,'conditions':conditions,'exceptions':exceptions}
        else:
            state='context' if block.get('is_heading') else 'example' if example else 'definition' if definition else 'needs_review'
            ledger.append(dict(locator=loc,state=state,reason='No safe obligation extraction' if state=='needs_review' else state))
            if text.rstrip().endswith(':') and not example:
                parent={'locator':loc,'modality':'mandatory' if current_profile else 'unknown',
                        'conditions':[{'kind':'unknown','text':text,'citation':citation(block)}],'exceptions':[]}
    # Parent enumerations are composite checks, not independent unrelated snippets.
    card_by_loc={c['locator']:c for c in cards}
    for card in cards:
        for dep in card['dependencies']:
            if dep['relation']=='parent_condition' and dep['target'] in card_by_loc:
                owner=card_by_loc[dep['target']]
                owner.setdefault('child_requirements',[]).append(card['id'])
                owner['dependencies'].append(dict(relation='enumeration_item',target=card['locator'],required=True,unresolved=False))
                cite=citation(fragments[card['locator']])
                if cite not in owner['citations']:owner['citations'].append(cite)
                owner['state']='needs_review' if owner['state']!='example' else 'example'
                owner['validation']['completeness']['semantic']='unknown'
                if 'composite_enumeration' not in owner['validation']['completeness']['reasons']:
                    owner['validation']['completeness']['reasons'].append('composite_enumeration')
    common_basis=[citation(b) for b in blocks if b.get('is_heading') and not any(p['basis'][0]['locator'] in b.get('heading_refs',[]) for p in profiles)]
    if not common_basis:
        common_basis=[citation(b) for b in blocks[:1] if b['exact_text']]
    if common_basis:
        profiles.insert(0,dict(id=common_id,name='Общие требования выбранного источника',kind='common',inherits=[],version=1,
            basis=common_basis,expression={'fact':{'name':'selected_sources','in':[source_id]}}))
    return dict(version=EXTRACTOR_VERSION,source_id=source_id,source_sha256=source['sha256'],profiles=profiles,
                cards=cards,coverage=ledger,coverage_audit=validators.audit_coverage(fragments,cards,ledger))
