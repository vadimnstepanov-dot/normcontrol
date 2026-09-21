import copy
import re
import zipfile
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as E
from .common import digest

def inspect_file(path, max_file=50*1024**2, max_unpacked=200*1024**2):
    path=Path(path).resolve(strict=True)
    if path.suffix.lower()!='.docx':raise ValueError('Рабочие документы принимаются в DOCX. Для DOC используйте отдельную безопасную конвертацию.')
    if path.stat().st_size>max_file:raise ValueError('Превышен лимит размера файла')
    with zipfile.ZipFile(path) as z:
        entries=z.infolist()
        if len(entries)>10000 or sum(x.file_size for x in entries)>max_unpacked:raise ValueError('Превышен лимит распаковки')
        for x in entries:
            if x.filename.lower().endswith('vbaproject.bin'):raise ValueError('Макросы запрещены')
            if x.filename.endswith('.xml'):
                raw=z.read(x)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('DTD и XML entities запрещены')
        if 'word/document.xml' not in z.namelist():raise ValueError('Нет тела Word')
    return path

def classify(blocks,cat):
    # Title evidence precedes TOC and body references to other deliverables.
    front=[]
    for b in blocks[:120]:
        if re.fullmatch(r'\s*СОДЕРЖАНИЕ\s*',b['text'],re.I):break
        front.append(b)
    scored=[]
    for p in cat['profiles']:
        words=re.findall(r'[а-яё]+',p['name'].lower())
        pattern=r'\s+'.join(re.escape(w) for w in words)
        for b in front:
            if re.search(pattern,b['text'].lower().replace('ё','е')):
                score=len(words)+(3 if len(b['text'])<len(p['name'])+30 else 0)
                if p['code']=='ЧТЗ':score+=2
                scored.append((score,p,b))
    scored.sort(key=lambda x:x[0],reverse=True)
    if not scored:return {'profile_id':None,'type':'не определён','confidence':0,'basis':[]}
    _,p,b=scored[0]
    return {'profile_id':p['id'],'type':p['code'],'confidence':.95,'basis':[{'locator':b['locator'],'quote':b['text']}], 'name':p['name']}

def parse(path,cat):
    from quality_review import units_for_document
    from word_compact import N,Q,plain
    path=inspect_file(path);d,legacy,oldprofile,units=units_for_document(str(path))
    blocks=[copy.deepcopy(b) for u in units for b in u['blocks']]
    styles=E.fromstring(d['files'].get('word/styles.xml',b'<empty/>'))
    style_names={s.get(Q+'styleId'):s.find('w:name',N).get(Q+'val','') for s in styles.findall('w:style',N) if s.find('w:name',N) is not None}
    did=d['sha256'][:20]
    # Efficient direct paragraph/cell map and multilevel column headers.
    cells={};table_meta={}
    for ti,t in enumerate(d['root'].findall('.//w:tbl',N),1):
        header_rows=[];table_rows=[];vertical={}
        for ri,row in enumerate(t.findall('w:tr',N),1):
            cs=[];col=1
            for ci,cell in enumerate(row.findall('w:tc',N),1):
                span=cell.find('w:tcPr/w:gridSpan',N);width=int(span.get(Q+'val','1')) if span is not None else 1
                vm=cell.find('w:tcPr/w:vMerge',N);merge=vm.get(Q+'val','continue') if vm is not None else ''
                text=' / '.join(plain(p) for p in cell.findall('w:p',N))
                if merge=='restart':vertical[col]=(ri,text)
                elif not merge:vertical.pop(col,None)
                info={'table':ti,'row':ri,'cell':ci,'column':col,'span':width,'vertical_merge':merge,'merge_origin':vertical.get(col),'text':text}
                cs.append(info)
                for p in cell.findall('.//w:p',N):cells[id(p)]=info
                col+=width
            is_header=row.find('w:trPr/w:tblHeader',N) is not None
            if is_header or ri==1:header_rows.append(cs)
            table_rows.append(cs)
        headers={}
        for hr in header_rows:
            for c in hr:
                for col in range(c['column'],c['column']+c['span']):headers.setdefault(col,[]).append(c['text'])
        table_meta[ti]={'headers':headers,'header_rows':[r[0]['row'] for r in header_rows if r],'rows':table_rows}
    para_cells={r['id']:cells[id(p)] for p,r in zip(d['paragraphs'],d['records']) if id(p) in cells}
    front_section=None;front_paragraph=0
    for b in blocks:
        b['document']=did;b['file']=path.name
        b.setdefault('address',b.get('section_title','Вспомогательная часть')+'; '+b['locator'])
        style_id=b.get('format',{}).get('style','')
        style_name=style_names.get(style_id,style_id)
        # Word's "TOC Heading" style is also used for ANNOTATION in real files.
        # Only TOC entries/its actual title are navigation, not every use of that style.
        b['toc']=bool(re.fullmatch(r'\s*содержание\s*',b['text'],re.I) or re.search(r'^(?:toc|содержани[ея])\s*\d+$',style_name,re.I))
        front_title=re.fullmatch(r'\s*(Аннотация|Обозначения и сокращения|Термины и определения|Термины, определения и сокращения)\s*',b['text'],re.I)
        if front_title and not b.get('is_heading'):
            b['is_heading']=True;front_section=(b['locator'],front_title[1]);front_paragraph=0
            b['heading_path']=[front_title[1]];b['section']=b['locator'];b['address']='«'+front_title[1]+'»; заголовок'
        elif b.get('is_heading') or b['toc']:front_section=None
        elif front_section:
            b['section'],title=front_section;b['heading_path']=[title];front_paragraph+=1
            if not b.get('table_context'):b['address']='«'+title+'»; абзац '+str(front_paragraph)+' после заголовка'
        c=para_cells.get(b['locator'])
        if c:
            headers=table_meta[c['table']]['headers'];cols=[]
            for col in range(c['column'],c['column']+c['span']):cols.extend(headers.get(col,[]))
            column=' / '.join(dict.fromkeys(s for s in cols if s))
            b['table_context']={**b.get('table_context',{}),**{k:v for k,v in c.items() if k!='text'},'column_name':column,'header_rows':table_meta[c['table']]['header_rows']}
            b['address'] += ('; колонка «'+column+'»') if column else ''
        b['address']=path.name+' → '+b['address']
    profile=classify(blocks,cat)
    headings=[{'locator':b['locator'],'title':b['text'],'path':b.get('heading_path',[]),'address':b['address']} for b in blocks if b.get('is_heading')]
    front='\n'.join(b['text'] for b in blocks[:120])
    codes=list(dict.fromkeys(re.findall(r'\b\d[\d.\-]{5,}\.[А-ЯA-Z]{2,5}\b',front)))
    # An exact shared title/code is evidence of a relationship; fuzzy names are only questions.
    titles=[]
    for b in blocks[:120]:
        if b.get('toc') or re.search(r'СТО|ГОСТ|нормативн|стандарт',b['text'],re.I):continue
        for m in re.finditer(r'«([^»]{12,160})»',b['text']):
            prefix=b['text'][max(0,m.start()-80):m.start()]
            if re.search(r'систем|программ|проект|подсистем',prefix,re.I) or b['text'].strip()==m[0]:titles.append(m[1])
    titles=list(dict.fromkeys(titles))
    images=[];objects=[]
    for i,p in enumerate(d['paragraphs'],1):
        if p.find('.//w:drawing',N) is not None or p.find('.//w:pict',N) is not None:images.append('p'+str(i))
        if p.find('.//w:object',N) is not None:objects.append('p'+str(i))
    changes={'insertions':len(d['root'].findall('.//w:ins',N)),'deletions':len(d['root'].findall('.//w:del',N)),'mode':'final'}
    return {'id':did,'sha256':d['sha256'],'path':str(path),'name':path.name,'profile':profile,'blocks':blocks,'headings':headings,'tables':table_meta,'codes':codes,'titles':titles,'layout':legacy['layout'],'changes':changes,
        'coverage':{'images':[{'locator':p,'state':'unverified','reason':'Изображение требует визуального анализа'} for p in images], 'objects':[{'locator':p,'state':'unverified','reason':'Встроенный объект'} for p in objects], 'text':'extracted','numbering':'verified Word labels when available; otherwise heading path'},
        'structure_hash':digest([headings,table_meta,changes]),'sections':len(headings)}

def relationships(docs):
    index=defaultdict(set)
    for d in docs:
        for title in d['titles']:index[('title',title.casefold())].add(d['id'])
        for code in d['codes']:index[('code',re.sub(r'\.[А-ЯA-Z]+$','',code))].add(d['id'])
    edges={}
    for (kind,key),ids in index.items():
        if len(ids)>1:
            # One group, not all pair combinations: O(document count + memberships).
            k=tuple(sorted(ids));edges[k]={'documents':list(k),'basis':kind,'value':key,'status':'exact_common_identity'}
    linked={x for k in edges for x in k}
    return {'groups':list(edges.values()),'unlinked':[d['id'] for d in docs if d['id'] not in linked]}

def compact_block(b):
    out={k:b[k] for k in ('document','locator','offset','text') if k in b}
    # A citation's meaning depends on its section, including production/test scope.
    if b.get('heading_path'):out['heading_path']=b['heading_path']
    if b.get('is_heading') and b.get('address'):out['address']=b['address']
    if b.get('table_context'):out['table']={k:v for k,v in b['table_context'].items() if k in ('table','row','column','span','vertical_merge','column_name','title')}
    if b.get('is_heading'):out['is_heading']=True
    return out

def semantic_groups(doc):
    groups=[];current=[];section=None
    for b in doc['blocks']:
        if not b['text'].strip() or b.get('toc'):continue
        if current and b.get('section')!=section:groups.append(current);current=[]
        section=b.get('section');current.append(compact_block(b))
    if current:groups.append(current)
    return groups

def coalesce_groups(doc,target_chars=12000):
    """Retain complete sections; combine short siblings under a shared ancestor."""
    by={b['locator']:b for b in doc['blocks']};packs=[];current=[];parent=None;size=0
    for group in semantic_groups(doc):
        path=by[group[0]['locator']].get('heading_path',[]);key=tuple(path[:2]) if len(path)>2 else tuple(path[:1])
        n=sum(len(b['text'])+90 for b in group)
        if current and (key!=parent or size+n>target_chars):packs.append(current);current=[];size=0
        current.extend(group);size+=n;parent=key
    if current:packs.append(current)
    bounded=[]
    def fit(group):
        if sum(len(b['text'])+90 for b in group)<=target_chars:
            bounded.append(group);return
        try:parts=split_blocks(group)
        except ValueError:bounded.append(group);return
        for part in parts:fit(part)
    for pack in packs:fit(pack)
    return bounded

def split_blocks(blocks):
    if len(blocks)>1:
        # Prefer complete table rows; repeated metadata travels with every cell.
        middle=len(blocks)//2
        candidates=[i for i in range(1,len(blocks)) if (blocks[i].get('table',{}).get('table'),blocks[i].get('table',{}).get('row'))!=(blocks[i-1].get('table',{}).get('table'),blocks[i-1].get('table',{}).get('row')) or not blocks[i].get('table')]
        if candidates:middle=min(candidates,key=lambda i:abs(i-middle))
        return [blocks[:middle],blocks[middle:]]
    b=blocks[0];text=b['text'];mid=len(text)//2
    if len(text)<128:raise ValueError('Неразделимый объект не помещается')
    cut=text.rfind(' ',max(1,mid-200),mid+200)
    if cut<1:cut=mid
    return [[{**b,'text':text[:cut]}],[{**b,'text':text[cut:],'offset':b.get('offset',0)+cut}]]
