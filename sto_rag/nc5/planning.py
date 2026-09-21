"""Generic source-preserving obligations and bounded comparison planning."""
import re,copy
from collections import defaultdict
from difflib import SequenceMatcher
from .common import digest
from .documents import compact_block

NUMBER = re.compile(r'(?P<operator>не\s+(?:более|менее|больше|меньше|выше|ниже)|до|от)?\s*(?P<value>-?\d+(?:[ \u00a0]\d{3})*(?:[,.]\d+)?)\s*(?P<unit>%|мс|секунд(?:а|ы)?|сек\.?|с\.?|минут(?:а|ы)?|мин\.?|час(?:а|ов)?|ч\.?|сут(?:ок|ки)?|дн(?:ей|я)?|ГБ|МБ|КБ)?',re.I)

def block_key(block):
    """Stable identity for an exact source fragment used by internal planning."""
    return digest([block.get('document',''),block.get('locator',''),block.get('offset',0),block.get('text','')])[:20]

def document_registry(doc):
    """Extract immutable navigation and literal facts once, without an LLM call."""
    headings=[{k:h[k] for k in ('locator','title','address') if k in h} for h in doc.get('headings',[])]
    numbers=[];definitions=[]
    for block in doc.get('blocks',[]):
        if block.get('toc'):continue
        text=block.get('text','')
        for match in NUMBER.finditer(text):
            # Bare single digits are mostly numbering noise; keep them only with a
            # unit or a comparison operator.
            if not match['unit'] and not match['operator'] and len(match['value'].replace(' ',''))<2:continue
            numbers.append({'value':match['value'],'unit':match['unit'] or '',
                'operator':match['operator'] or '=','document':doc['id'],
                'locator':block['locator'],'address':block.get('address',''),
                'quote':text[max(0,match.start()-100):min(len(text),match.end()+100)]})
        definition=re.match(r'^\s*([А-ЯЁA-Z][А-ЯЁA-Z0-9_.-]{1,24})\s*[—–-]\s*(.{8,})$',text)
        if definition:
            definitions.append({'term':definition[1],'definition':definition[2],
                'document':doc['id'],'locator':block['locator'],'address':block.get('address','')})
    body={'version':1,'document':doc['id'],'structure':headings,'structure_sha256':digest(headings),'numbers':numbers,'definitions':definitions}
    body['sha256']=digest(body)
    return body

def focused_outline(doc,blocks,limit=24):
    """Return only navigation that explains supplied evidence, preserving order."""
    headings=doc.get('headings',[])
    if not headings:return []
    locators={b.get('section') for b in blocks}|{b.get('locator') for b in blocks if b.get('is_heading')}
    titles={title for b in blocks for title in b.get('heading_path',[])}
    chosen={i for i,h in enumerate(headings) if h.get('locator') in locators or h.get('title') in titles}
    # One neighbour on either side makes a section boundary visible without
    # retransmitting the entire document tree.
    chosen|={j for i in chosen for j in (i-1,i+1) if 0<=j<len(headings)}
    if not chosen:chosen=set(range(min(4,len(headings))))
    return [{k:headings[i][k] for k in ('locator','title','address') if k in headings[i]} for i in sorted(chosen)[:limit]]

def group_sto_evidence(entries,size=10,target_chars=52000):
    """Greedily pack rules that reuse exact evidence into one model request.

    Each entry contains one rule and evidence retrieved once. A group is created
    only when at least one source fragment is shared, so unrelated requirements do
    not contaminate each other's decision context.
    """
    size=max(1,min(16,int(size)));clusters=[]
    normalized=[]
    for entry in entries:
        item=dict(entry);item['keys']={block_key(b) for b in item.get('blocks',[])}
        item['chars']={block_key(b):len(b.get('text','')) for b in item.get('blocks',[])}
        normalized.append(item)
    # Larger evidence sets first make the saved overlap deterministic.
    for entry in sorted(normalized,key=lambda x:(-sum(x['chars'].values()),x['rule']['requirement_id'])):
        best=None;best_saved=0
        for cluster in clusters:
            if len(cluster['entries'])>=size:continue
            shared=entry['keys']&cluster['keys']
            saved=sum(entry['chars'].get(k,0) for k in shared)
            union=cluster['keys']|entry['keys']
            union_chars=sum({**cluster['chars'],**entry['chars']}.get(k,0) for k in union)
            if saved>best_saved and union_chars<=target_chars:best,best_saved=cluster,saved
        if best is None:
            clusters.append({'entries':[entry],'keys':set(entry['keys']),'chars':dict(entry['chars'])})
        else:
            best['entries'].append(entry);best['keys']|=entry['keys'];best['chars'].update(entry['chars'])
    result=[]
    for cluster in clusters:
        blocks=[];seen=set();searches=[]
        for entry in cluster['entries']:
            for block in entry.get('blocks',[]):
                key=block_key(block)
                if key not in seen:seen.add(key);blocks.append(block)
            searches.append(entry.get('search',{}))
        result.append({'rules':[x['rule'] for x in cluster['entries']],
            'blocks':blocks,'searches':searches,
            'evidence_map':{x['rule']['requirement_id']:sorted(x['keys']) for x in cluster['entries']}})
    return result

def atomic_cards(cards):
    result=[];mapping={}
    for card in cards:
        if card.get('check_stage') not in ('sto','inter'):
            result.append(card);mapping[card['requirement_id']]=[card['requirement_id']];continue
        text=card['source_quote'];units=[]
        # Lists stay attached to their introductory obligation and conditions.
        for line in re.split(r'(?<=[.!?])\s+(?=(?:Привод|Описыва|Указыва|Необходимо|Следует|Долж|Запрещ|Не допуска)[а-яё]*)|\n',text):
            if units and (re.match(r'^\s*[-–—•]',line) or units[-1].rstrip().endswith(':')):
                units[-1]+='\n'+line
            else:units.append(line)
        parts=[];prefix=[];offset=0
        for unit in units:
            raw=unit.strip();start=text.find(raw,offset) if raw else offset;offset=start+len(raw)
            if not raw:continue
            if not re.search(r'долж|следует|необходимо|привод|описыва|указыва|не допуска|запрещ|содерж|включа|предусматрив|устанавлива|рекоменду|выполня|оформля',raw,re.I):
                prefix.append({'locator':card['source_locator'],'quote':raw});continue
            # Keep conditional sentences intact; splitting is structural, not inferred legal semantics.
            parts.append((raw,start,list(prefix)));prefix=[]
        if len(parts)<2:
            result.append(card);mapping[card['requirement_id']]=[card['requirement_id']];continue
        mapping[card['requirement_id']]=[]
        for raw,start,context in parts:
            child=copy.deepcopy(card);rid=digest([card['requirement_id'],start,raw])[:20]
            child.update(requirement_id=rid,parent_requirement_id=card['requirement_id'],source_quote=raw,obligation=raw,compact_text=raw,source_char_offset=start,version=2)
            child['parent_context_refs']=card.get('parent_context_refs',[])+context
            # Retain all conditional/exception paragraphs of the original unit as context.
            child['parent_context_refs'] += [{'locator':card['source_locator'],'quote':u.strip()} for u in units if u.strip()!=raw and re.search(r'если|при наличии|при необходимости|за исключением|не распространя',u,re.I)]
            child['source_parent_quote']=text
            result.append(child);mapping[card['requirement_id']].append(rid)
    return result,mapping

def atomize_catalog(cat):
    cat=copy.deepcopy(cat);cards,mapping=atomic_cards(cat['cards']);cat['cards']=cards
    for p in cat['profiles']:
        for field in ('requirements','general'):p[field]=[x for rid in p.get(field,[]) for x in mapping.get(rid,[rid])]
    for row in cat['ledger']:row['requirement_ids']=[x for rid in row['requirement_ids'] for x in mapping.get(rid,[rid])]
    cat['interdocument']=[x for rid in cat['interdocument'] for x in mapping.get(rid,[rid])]
    cat['coverage']['semantic_review_pending']=len(cards);cat['atomic_parent_map']=mapping
    return cat

def completeness(doc,blocks):
    supplied={(b['locator'],b.get('offset',0),b['text']) for b in blocks if b['document']==doc['id']}
    bysection=defaultdict(list)
    for b in doc['blocks']:
        if not b.get('toc') and b['text'].strip():bysection[b.get('section','')].append(b)
    complete=[s for s,bs in bysection.items() if s and all((b['locator'],b.get('offset',0),b['text']) in supplied for b in bs)]
    return {'document':doc['id'],'complete_sections':complete,'section_blocks':{s:[b['locator'] for b in bysection[s]] for s in complete},'document_complete':False,'absence_scope':'Only listed sections are complete; global semantic absence is not proven.'}

def table_rows(doc):
    rows=defaultdict(list)
    for b in doc['blocks']:
        t=b.get('table_context',{})
        if t and not b.get('toc'):rows[(t['table'],t['row'])].append(b)
    return rows

def norm(s):return re.sub(r'[^а-яa-z0-9]+',' ',s.casefold().replace('ё','е')).strip()

def column_key(s):
    s=norm(s)
    if re.search(r'^№|номер|^поток|^связ',s) or 'потока код' in s:return 'number'
    if 'источник' in s:return 'source'
    if 'получател' in s:return 'target'
    if 'состав дан' in s:return 'data'
    if 'инициатор' in s:return 'initiator'
    if 'периодичност' in s or 'временной регламент' in s:return 'frequency'
    if 'объем' in s and 'дан' in s:return 'volume'
    if 'протокол' in s and 'взаимодейств' in s:return 'protocol'
    return s

def column_findings(doc,rule,blocks):
    text=rule['source_quote']
    if 'со следующими столбцами' not in text:return []
    expected=re.findall('«([^»]+)»',text.split('со следующими столбцами',1)[1])
    if len(expected)<2:return []
    chosen={b.get('table',{}).get('table') for b in blocks if b.get('table')}
    result=[]
    for (table,row),bs in table_rows(doc).items():
        if table not in chosen or row!=1:continue
        actual=bs[0]['table_context'].get('headers',[]);keys={column_key(x) for x in actual}
        # A schema must first match the identity columns, not an arbitrary nearby table.
        if not {'source','target','data'}<=keys:continue
        missing=[s for s in expected if column_key(s) not in keys]
        if not missing:continue
        result.append({'category':'соответствие СТО','severity':'major','kind':'violation','issue':'В таблице отсутствуют обязательные колонки: '+', '.join(missing),'explanation':'Полный перечень заголовков таблицы сопоставлен с явно заданным перечнем колонок нормы. Не представлены: '+', '.join(missing)+'.','suggestion':'Добавить указанные параметры и заполнить их для применимых строк. Если сведения вынесены отдельно, обеспечить допустимую нормой однозначную связь.','evidence':[{'document':doc['id'],'locator':b['locator'],'quote':b['text']} for b in bs if b['text'].strip()],'requirement_id':rule['requirement_id'],'search_query':'','evidence_scope':'quoted_cell','comparison_method':'complete_table_headers','defect_key':digest(['headers',doc['id'],table,sorted(map(column_key,missing))])})
    return result

def deterministic_rule_check(doc,rule,blocks):
    """Return source-backed coverage when no semantic model judgement is needed.

    Only exact, closed-world properties are accepted here.  An inconclusive local
    check returns None and the requirement continues through the LLM stage.
    """
    text=rule['source_quote']
    if 'со следующими столбцами' in text:
        expected=re.findall('«([^»]+)»',text.split('со следующими столбцами',1)[1])
        chosen={b.get('table',{}).get('table') for b in blocks if b.get('table')}
        if len(expected)>=2:
            for (table,row),bs in table_rows(doc).items():
                if table not in chosen or row!=1:continue
                actual=bs[0]['table_context'].get('headers',[]);keys={column_key(x) for x in actual}
                if not {'source','target','data'}<=keys:continue
                missing=[s for s in expected if column_key(s) not in keys]
                return {
                    'document':doc['id'],'requirement_id':rule['requirement_id'],'state':'checked',
                    'reason':'Полный набор заголовков таблицы сопоставлен программно с закрытым перечнем нормы'+
                             (': отсутствуют '+', '.join(missing) if missing else '; все обязательные колонки присутствуют'),
                    'method':'deterministic_complete_table_headers','locators':[b['locator'] for b in bs],
                }
    # A title-only card can be closed by an exact heading from the full Word tree.
    normative=r'долж|следует|необходимо|привод|описыва|указыва|не допуска|запрещ|содерж|включа|предусматрив|устанавлива|выполня|оформля'
    if len(text)<=240 and not re.search(normative,text,re.I):
        expected=norm(re.sub(r'^\d+(?:\.\d+)*\s*','',rule.get('expected_evidence','') or text))
        for heading in doc.get('headings',[]):
            if expected and norm(re.sub(r'^\d+(?:\.\d+)*\s*','',heading['title']))==expected:
                return {'document':doc['id'],'requirement_id':rule['requirement_id'],'state':'checked',
                        'reason':'Точный обязательный заголовок найден программно в полном дереве Word',
                        'method':'deterministic_exact_heading','locators':[heading['locator']]}
    return None

def group_sto_rules(cards,size=4):
    """Batch obligations that target the same evidence without mixing scopes."""
    grouped={};order=[];size=max(1,min(6,int(size)))
    for card in cards:
        focus='columns' if 'со следующими столбцами' in card['source_quote'] else 'front' if re.search(r'титульн|редакци.*предыдущ',card['source_quote'],re.I) else 'content'
        key=(card.get('document_name',''),card.get('appendix',''),card.get('expected_evidence',''),card.get('normative_kind',''),focus)
        if key not in grouped:grouped[key]=[];order.append(key)
        grouped[key].append(card)
    return [items[start:start+size] for key in order for items in [grouped[key]] for start in range(0,len(items),size)]

def comparison_payloads(docs):
    """Group source rows by identity and scope, never by reference findings."""
    groups=defaultdict(list)
    for d in docs:
        for (table,row),bs in table_rows(d).items():
            if row==1:continue
            columns=defaultdict(list)
            for b in bs:columns[column_key(b['table_context'].get('column_name',''))].append(b['text'])
            if not {'number','source','target','data'}<=columns.keys():continue
            title=bs[0]['table_context'].get('title','')
            env='test' if re.search('тестов',title,re.I) else 'prod' if re.search('продуктив|промышлен',title,re.I) else ''
            if not env:continue
            key=(norm(' '.join(columns['number'])),norm(' '.join(columns['source'])),norm(' '.join(columns['target'])),env)
            values=sorted({norm(x) for x in columns['data'] if norm(x)})
            groups[key].append((d,bs,values))
    for key,rows in groups.items():
        for i,(a,abs,av) in enumerate(rows):
            for b,bbs,bv in rows[i+1:]:
                if a['id']==b['id'] or av==bv:continue
                yield {'stage':'inter','requirements':[],'blocks':[compact_block(x) for x in abs+bbs],'scope':'complete_matched_table_rows','computed_comparison':{'identity':key,'only_left':sorted(set(av)-set(bv)),'only_right':sorted(set(bv)-set(av)),'interpretation':'Exact text set differences; synonyms, detail levels and applicability require semantic checking.'},'documents':[{'id':d['id'],'profile':d['profile']} for d in (a,b)]}
    # Named scalar parameters: compare original clauses, keep roles/conditions for model adjudication.
    scalars=defaultdict(list)
    for d in docs:
        for b in d['blocks']:
            if b.get('toc') or b.get('is_heading'):continue
            if not re.search(r'не более|не менее|максимальн|минимальн',b['text'],re.I):continue
            labels=set(re.findall(r'\b[A-Z][A-Z0-9_]{1,11}\b',b['text']))
            for label in labels:
                if re.search(r'\d+\s*(?:час|секунд|минут|сут|мс|ГБ|МБ|%)',b['text'],re.I):scalars[label].append((d,b))
    for label,items in scalars.items():
        for i,(a,ab) in enumerate(items):
            for b,bb in items[i+1:]:
                if a['id']==b['id'] or ab['text']==bb['text']:continue
                yield {'stage':'inter','requirements':[],'scope':'named_parameter_pair','parameter':label,'computed_bounds':compare_bounds(ab['text'],bb['text']),'documents':[{'id':d['id'],'profile':d['profile']} for d in (a,b)],'blocks':[compact_block(ab),compact_block(bb)],'directions':'Сопоставь роли документов, объект, единицы, условия и направление ограничения. Различай совместимость границ и обеспечение исходного требования проектным решением. Название параметра само по себе не доказывает одинаковую область действия.'}

def duplicate_groups(findings):
    keep=[];duplicates=[]
    for f in findings:
        loc={(e['document'],e['locator']) for e in f['evidence']}
        match=next((x for x in keep if x.get('requirement_id','')==f.get('requirement_id','') and x.get('category')==f.get('category') and {(e['document'],e['locator']) for e in x['evidence']}==loc and (x.get('defect_key') and x.get('defect_key')==f.get('defect_key') or norm(x.get('suggestion',''))==norm(f.get('suggestion','')) and SequenceMatcher(None,norm(x['issue']),norm(f['issue'])).ratio()>=.9)),None)
        if not match and len(norm(f.get('suggestion','')))>24:
            # Cross-stage duplicates require identical quoted evidence and a
            # substantive identical correction, not just a common paragraph.
            quoted={(e['document'],e['locator'],e['quote']) for e in f['evidence']}
            match=next((x for x in keep if x.get('requirement_id','')==f.get('requirement_id','') and norm(x.get('suggestion',''))==norm(f.get('suggestion','')) and {(e['document'],e['locator'],e['quote']) for e in x['evidence']}==quoted),None)
        if match:duplicates.append((f,match))
        else:keep.append(f)
    return keep,duplicates


def compare_bounds(left,right):
    from decimal import Decimal
    def read(text):
        hits=re.findall(r'(не более|не менее)\s*(\d+(?:[,.]\d+)?)\s*(час(?:а|ов)?|ч\b|секунд(?:а|ы)?|с\b|минут(?:а|ы)?|мин\b)',text,re.I)
        if len(hits)!=1:return None
        op,value,unit=hits[0];factor=3600 if unit.casefold().startswith(('ч','час')) else 60 if unit.casefold().startswith('мин') else 1
        return {'operator':'<=' if op.casefold()=='не более' else '>=','seconds':str(Decimal(value.replace(',','.'))*factor)}
    a,b=read(left),read(right)
    if not a or not b:return {'state':'ambiguous','requires_semantic_check':True}
    av,bv=Decimal(a['seconds']),Decimal(b['seconds'])
    compatible=(a['operator']==b['operator'] or (av>=bv if a['operator']=='<=' else bv>=av))
    subset=None
    if a['operator']==b['operator']:subset=bv<=av if a['operator']=='<=' else bv>=av
    return {'left':a,'right':b,'compatible_if_same_scope':compatible,'right_implies_left_if_same_scope':subset,'requires_semantic_check':True,'note':'Document roles, object identity and conditions must be established before classifying a defect.'}


def coalesce_row_findings(findings,payload):
    if payload.get('scope')!='complete_matched_table_rows' or not payload.get('computed_comparison'):return findings
    if len(findings)<2:return findings
    if any(not isinstance(f,dict) or any(not isinstance(f.get(k),str) for k in ('issue','explanation','suggestion','kind')) or not isinstance(f.get('evidence'),list) or any(not isinstance(e,dict) or any(not isinstance(e.get(k),str) for k in ('document','locator','quote')) for e in f['evidence']) for f in findings):return findings
    groups=defaultdict(list)
    for f in findings:groups[(f.get('category'),f.get('requirement_id',''))].append(f)
    result=[]
    for key,fs in groups.items():
        if len(fs)==1:result+=fs;continue
        merged=dict(fs[0]);merged['issue']='Расхождения в описании одного информационного потока'
        merged['explanation']='\n'.join(dict.fromkeys(f['issue']+': '+f['explanation'] for f in fs))
        merged['suggestion']='\n'.join(dict.fromkeys(f['suggestion'] for f in fs))
        merged['evidence']=list({(e['document'],e['locator'],e['quote']):e for f in fs for e in f['evidence']}.values())
        merged['kind']='question' if any(f['kind']=='question' for f in fs) else 'violation'
        merged['defect_key']=digest(['row-comparison',payload['computed_comparison']['identity'],sorted({e['document'] for e in merged['evidence']})])
        merged['merged_items']=len(fs);result.append(merged)
    return result


def coverage_questions(raw,payload):
    """Surface a localized coverage gap as a question, never as an automatic violation."""
    if payload.get('stage')!='sto' or payload.get('validation_retry'):return []
    inventory=payload.get('source_inventory',[])
    if isinstance(inventory,dict):inventory=[inventory]
    target=set(payload.get('search',{}).get('sections',[]))
    allowed={loc for i in inventory for section,locs in i.get('section_blocks',{}).items() if section in target for loc in locs}
    if not allowed:return []
    rules={r['requirement_id']:r for r in payload.get('requirements',[])}
    present={f.get('requirement_id') for f in raw.get('findings',[]) if isinstance(f,dict)};result=[]
    for c in raw.get('coverage',[]):
        if not isinstance(c,dict) or c.get('state')!='insufficient' or c.get('requirement_id') in present:continue
        rid=c.get('requirement_id');reason=c.get('reason','')
        if rid not in rules or not isinstance(reason,str) or not re.search(r'отсутств|не описан|не привед|не раскрыт',reason,re.I):continue
        bs=[b for b in payload.get('blocks',[]) if b.get('text','').strip() and b['locator'] in allowed and not b.get('is_heading')]
        if not bs:continue
        b=bs[0]
        result.append({'category':'соответствие СТО','severity':'major','kind':'question','requirement_id':rid,'issue':'Требует проверки полнота выполнения отдельного требования СТО','explanation':reason+' Это вопрос о полноте, а не подтверждённое отсутствие сведений во всём документе.','suggestion':'Проверить указанный пробел и адресные ссылки на другие разделы; при отсутствии требуемого содержания дополнить документ.','evidence':[{'document':b['document'],'locator':b['locator'],'quote':b['text']}],'search_query':rules[rid]['source_quote'],'coverage_followup':True})
    return result
