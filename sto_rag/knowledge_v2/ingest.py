"""Isolated document intake for v2. Never imports legacy catalog or caches."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from zipfile import BadZipFile
from word_source import open_archive as ZipFile

from .store import KnowledgeStore, Conflict, checksum

PARSER_VERSION='structure-v2.2'
MAX_FILE=50*1024*1024
MAX_UNPACKED=200*1024*1024
MAX_ENTRIES=10000
OLE=b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def inspect(path, filename=None):
    path=Path(path);name=filename or path.name
    if not isinstance(name,str) or not name or len(name)>240 or '/' in name or '\\' in name or '\x00' in name:
        raise ValueError('Unsafe source filename')
    kind=Path(name).suffix.casefold()
    if kind not in ('.docx','.doc','.pdf') or not 0<path.stat().st_size<=MAX_FILE:
        raise ValueError('Unsupported type or size')
    with path.open('rb') as f:signature=f.read(8)
    if kind=='.docx':
        if not signature.startswith(b'PK'):raise ValueError('DOCX signature')
        try:
            with ZipFile(path) as z:
                entries=z.infolist(); names=[x.filename for x in entries]
                if len(entries)>MAX_ENTRIES or sum(x.file_size for x in entries)>MAX_UNPACKED or 'word/document.xml' not in names:
                    raise ValueError('DOCX structure/expanded size')
                for info in entries:
                    if (info.filename.startswith('/') or '\\' in info.filename or '..' in info.filename.split('/')
                        or info.filename.lower().endswith('vbaproject.bin') or (info.flag_bits & 1)):
                        raise ValueError('Unsafe DOCX member')
                    if info.filename.endswith(('.xml','.rels')):
                        tail=b''
                        with z.open(info) as member:
                            for chunk in iter(lambda:member.read(1024*1024),b''):
                                scanned=(tail+chunk).upper()
                                if b'<!DOCTYPE' in scanned or b'<!ENTITY' in scanned:raise ValueError('XML DTD/entities')
                                tail=scanned[-16:]
        except BadZipFile as exc:raise ValueError('Invalid DOCX') from exc
    elif kind=='.doc' and signature!=OLE:raise ValueError('DOC signature')
    elif kind=='.pdf' and not signature.startswith(b'%PDF-'):raise ValueError('PDF signature')
    return kind


def normalize(text):
    return re.sub(r'\s+',' ',text.replace('\u00ad','').replace('\xa0',' ')).strip()


def element_text(element):
    return ''.join((node.text or '') if node.tag==W+'t' else '\t' if node.tag==W+'tab' else '\n'
                   for node in element.iter() if node.tag in (W+'t',W+'tab',W+'br')).strip()


def classify(blocks):
    matches=[]
    patterns=[('sto',r'^(?:сто\s+ржд|стандарт\s+организации\b)'),
              ('gost',r'^гост(?:\s+р)?\b'),('instruction',r'^инструкция\b'),
              ('regulation',r'^регламент\b')]
    for block in blocks[:35]:
        if block['kind'] not in ('paragraph','table_cell'):continue
        line=normalize(block['exact_text']).casefold()
        if len(line)>180:continue
        for code,pattern in patterns:
            if re.search(pattern,line):matches.append((code,block['locator'],block['exact_text'][:180]))
    kinds={code for code,_,_ in matches}
    if len(kinds)==1:
        code,locator,quote=matches[0]
        return {'type':code,'evidence':{'locator':locator,'quote':quote}}
    return {'type':'unknown','evidence':'ambiguous-or-absent'}


def parse_docx(path):
    blocks=[];coverage=[];headings=[];counts={'paragraphs':0,'tables':0,'images':0,'notes':0}
    table_label=''; heading_stack=[]
    with ZipFile(path) as z:
        root=ET.fromstring(z.read('word/document.xml'))
        styles={}
        if 'word/styles.xml' in z.namelist():
            styles={s.get(W+'styleId'):s for s in ET.fromstring(z.read('word/styles.xml'))}
        def inherited(p):
            item=p.find(f'{W}pPr/{W}pStyle');sid=item.get(W+'val','') if item is not None else ''
            chain=[];seen=set()
            while sid in styles and sid not in seen:
                seen.add(sid);s=styles[sid];chain.insert(0,s.find(W+'pPr'))
                parent=s.find(W+'basedOn');sid=parent.get(W+'val','') if parent is not None else ''
            chain.append(p.find(W+'pPr'));props={}
            for pr in chain:
                if pr is None:continue
                for key,route in [('outline',W+'outlineLvl'),('num',f'{W}numPr/{W}numId'),('ilvl',f'{W}numPr/{W}ilvl')]:
                    node=pr.find(route)
                    if node is not None:props[key]=node.get(W+'val','')
            return props
        if 'word/numbering.xml' in z.namelist():
            coverage.append(dict(locator='document/numbering',state='needs_review',
                                 reason='Automatic numbering may need Word-rendered labels'))
        if 'word/comments.xml' in z.namelist():
            coverage.append(dict(locator='document/comments',state='needs_review',reason='Word comments are not normative text'))
        body=root.find(W+'body')
        if body is None:raise ValueError('DOCX body missing')
        for child in body:
            if child.tag==W+'p':
                counts['paragraphs']+=1;p=counts['paragraphs'];raw=element_text(child)
                style=child.find(f'{W}pPr/{W}pStyle');style_id=style.get(W+'val','') if style is not None else ''
                props=inherited(child)
                level=int(props.get('outline','9'))
                explicit=re.match(r'^((?:\d+|[А-ЯA-Z])(?:\.\d+)*(?:\.)?)\s+\S',raw)
                is_heading=(level<9 or bool(re.match(r'^(?:Heading|Заголовок)\s*\d*$',style_id,re.I))
                            or bool(re.match(r'^Приложение\s+[А-ЯA-Z]\b',raw,re.I)))
                if is_heading:
                    if level>=9:level=0
                    # Numbered template headings often share a single Word outline level.
                    # Their literal hierarchy is more specific than that shared style.
                    if explicit and level>=5 and props.get('num') in (None,'0'):
                        level=max(0,explicit.group(1).rstrip('.').count('.'))+5
                    if re.search(r'Шаблон\s+документ',raw,re.I):
                        level=4;heading_stack=[]
                    while heading_stack and heading_stack[-1][0]>=level:heading_stack.pop()
                    heading_stack.append((level,raw,f'p{p}'))
                    headings=[h[1] for h in heading_stack]
                if raw:
                    locator=f'p{p}'
                    if re.match(r'^(?:Таблица|Таблицы|Продолжение таблицы)\s+[А-ЯA-Z]?\.?\d',raw,re.I):table_label=raw
                    blocks.append(dict(locator=locator,kind='paragraph',exact_text=raw,search_text=normalize(raw).casefold(),
                                       heading_path=list(headings),clause=explicit.group(1).rstrip('.') if explicit else '',
                                       paragraph=p,style=style_id,is_heading=is_heading,heading_refs=[h[2] for h in heading_stack],
                                       numbering=props,
                                       cross_refs=re.findall(r'\b(?:пункт|таблиц[аеуы]|рисунк[аеу])\s+[А-ЯA-Z]?\.?\d+(?:\.\d+)*',raw,re.I)))
                if props.get('num') not in (None,'0'):
                    coverage.append(dict(locator=f'p{p}/numbering',state='needs_review',reason='Automatic Word numbering not independently verified'))
                for drawing in child.iter():
                    if drawing.tag in (W+'drawing',W+'pict'):
                        counts['images']+=1
                        coverage.append(dict(locator=f'p{p}/image{counts["images"]}',state='unreadable',reason='Visual object; OCR/vision required'))
                    elif drawing.tag==W+'object':
                        coverage.append(dict(locator=f'p{p}/object',state='unreadable',reason='Embedded object not parsed'))
            elif child.tag==W+'tbl':
                counts['tables']+=1;ti=counts['tables'];rows=child.findall(W+'tr');headers=[];vertical={}
                for ri,row in enumerate(rows,1):
                    cells=[];column=1
                    for ci,cell in enumerate(row.findall(W+'tc'),1):
                        if cell.find('.//'+W+'tbl') is not None:
                            coverage.append(dict(locator=f't{ti}/r{ri}/c{column}/nested-table',state='needs_review',
                                                 reason='Nested table text retained; inner cell topology needs verification'))
                        raw='\n'.join(element_text(p) for p in cell.findall('.//'+W+'p')).strip()
                        tcpr=cell.find(W+'tcPr');span=tcpr.find(W+'gridSpan') if tcpr is not None else None
                        width=max(1,int(span.get(W+'val','1'))) if span is not None else 1
                        merge=tcpr.find(W+'vMerge') if tcpr is not None else None
                        mode=merge.get(W+'val','continue') if merge is not None else None
                        if mode=='restart':vertical[column]=(ri,raw)
                        origin=vertical.get(column) if mode=='continue' else None
                        if mode is None:vertical.pop(column,None)
                        cells.append(dict(column=column,cell=ci,span=width,text=raw,merge_origin=origin))
                        column+=width
                    is_header=ri==1 or row.find(f'{W}trPr/{W}tblHeader') is not None
                    if is_header:headers.append(cells)
                    for cell in cells:
                        locator=f't{ti}/r{ri}/c{cell["column"]}'
                        column_headers=[c['text'] for hr in headers for c in hr if c['column']<=cell['column']<c['column']+c['span'] and c['text']]
                        exact=cell['text']
                        blocks.append(dict(locator=locator,kind='table_cell',exact_text=exact,search_text=normalize(exact).casefold(),
                                           heading_path=list(headings),heading_refs=[h[2] for h in heading_stack],table=ti,row=ri,column=cell['column'],span=cell['span'],
                                           header_path=list(dict.fromkeys(column_headers)),merge_origin=cell['merge_origin'],table_label=table_label))
                        if not exact and cell['merge_origin'] is None and not is_header:
                            coverage.append(dict(locator=locator,state='needs_review',reason='Empty table cell'))
                    for drawing in row.iter():
                        if drawing.tag in (W+'drawing',W+'pict'):
                            counts['images']+=1
                            coverage.append(dict(locator=f't{ti}/r{ri}/image{counts["images"]}',state='unreadable',reason='Visual object; OCR/vision required'))
                        elif drawing.tag==W+'object':
                            coverage.append(dict(locator=f't{ti}/r{ri}/object',state='unreadable',reason='Embedded object not parsed'))
                table_label=''
        for part in ('footnotes','endnotes'):
            name='word/'+part+'.xml'
            if name in z.namelist():
                for note in ET.fromstring(z.read(name)):
                    nid=note.get(W+'id','')
                    if nid.lstrip('-').isdigit() and int(nid)>0:
                        exact=element_text(note)
                        if exact:
                            counts['notes']+=1
                            blocks.append(dict(locator=f'{part}/{nid}',kind='note',exact_text=exact,
                                               search_text=normalize(exact).casefold(),heading_path=[]))
        if root.findall('.//'+W+'del') or root.findall('.//'+W+'ins'):
            coverage.append(dict(locator='document/revisions',state='needs_review',reason='Tracked changes; extracted current markup only'))
    return blocks,coverage,counts


def parse_pdf(path,ocr=True):
    try:from pypdf import PdfReader
    except ImportError as exc:raise RuntimeError('pypdf is required for PDF extraction') from exc
    reader=PdfReader(str(path),strict=False)
    if reader.is_encrypted:raise ValueError('Encrypted PDF')
    if len(reader.pages)>2000:raise ValueError('PDF page limit')
    blocks=[];coverage=[];counts={'pages':len(reader.pages),'ocr_pages':0}
    for i,page in enumerate(reader.pages,1):
        text=page.extract_text() or ''
        if not text.strip() and ocr and shutil.which('pdftoppm') and shutil.which('tesseract'):
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp)/'page';image=Path(tmp)/'page.png'
                try:
                    subprocess.run(['pdftoppm','-f',str(i),'-l',str(i),'-singlefile','-png','-r','180',str(path),str(base)],
                                   check=True,timeout=90,capture_output=True)
                    text=subprocess.run(['tesseract',str(image),'stdout','-l','rus+eng'],check=True,timeout=90,
                                        capture_output=True,text=True).stdout
                    if text.strip():counts['ocr_pages']+=1
                except (OSError,subprocess.SubprocessError):pass
        if not text.strip():
            coverage.append(dict(locator=f'page/{i}',state='unreadable',reason='No text layer; OCR unavailable or unsuccessful'))
            continue
        if counts['ocr_pages'] and not (page.extract_text() or '').strip():
            coverage.append(dict(locator=f'page/{i}',state='needs_review',reason='OCR text requires visual verification'))
        for j,paragraph in enumerate(re.split(r'\n\s*\n',text),1):
            exact=paragraph.strip()
            if exact:
                blocks.append(dict(locator=f'page/{i}/block/{j}',kind='pdf_text',exact_text=exact,
                                   search_text=normalize(exact).casefold(),heading_path=[],page=i))
    return blocks,coverage,counts


def convert_doc(path):
    with tempfile.TemporaryDirectory(prefix='knowledge-doc-convert-') as tmp:
        source=Path(tmp)/'source.doc';shutil.copyfile(path,source)
        if shutil.which('soffice'):
            cmd=['soffice','-env:UserInstallation=file://'+str(Path(tmp)/'profile'),'--headless','--convert-to','docx',
                 '--outdir',tmp,str(source)]
        elif os.name=='nt':
            script=Path(__file__).resolve().parents[1]/'nc5'/'word_export.ps1'
            cmd=['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),
                 '-Source',str(source),'-Destination',str(Path(tmp)/'source.docx'),'-Format','docx']
        else:raise RuntimeError('DOC converter is unavailable')
        try:subprocess.run(cmd,check=True,timeout=180,capture_output=True)
        except (OSError,subprocess.SubprocessError) as exc:raise RuntimeError('DOC conversion failed') from exc
        target=Path(tmp)/'source.docx';inspect(target,'source.docx')
        return parse_docx(target)


def parse(path,filename=None):
    kind=inspect(path,filename)
    if kind=='.docx':blocks,coverage,counts=parse_docx(path)
    elif kind=='.doc':blocks,coverage,counts=parse_docx(path)
    else:blocks,coverage,counts=parse_pdf(path)
    classification=classify(blocks)
    if classification['type']=='unknown':
        coverage.append(dict(locator='document/type',state='needs_review',reason='Document type requires explicit profile choice'))
    if kind=='.pdf':
        coverage.append(dict(locator='document/pdf-layout',state='needs_review',reason='PDF text has no verified table cell or heading structure'))
    sections={}
    for block in blocks:
        key=json.dumps([block.get('heading_path',[]),block.get('page')],ensure_ascii=False)
        sections.setdefault(key,[]).append(block['exact_text'])
    section_hashes={key:hashlib.sha256(json.dumps(text,ensure_ascii=False).encode()).hexdigest() for key,text in sections.items()}
    for block in blocks:
        block['context_hash']=hashlib.sha256(json.dumps([section_hashes[json.dumps([block.get('heading_path',[]),block.get('page')],ensure_ascii=False)],
            block.get('header_path',[]),block.get('merge_origin'),block.get('exact_text')],ensure_ascii=False).encode()).hexdigest()
        coverage.append(dict(locator=block['locator'],state='context' if block.get('is_heading') else 'needs_review',
                             reason='Heading context' if block.get('is_heading') else 'Requires normative interpretation in stage 4'))
    return dict(parser_version=PARSER_VERSION+('+native-doc-v1' if kind=='.doc' else ''),kind=kind,classification=classification,blocks=blocks,coverage=coverage,counts=counts)


def ingest(store:KnowledgeStore,set_id,source_id,filename,sha,stream,supersedes=None):
    """Stream an authorized portal source to immutable local storage, then parse it.

    Repeating an operation is safe even after process death between file and DB writes.
    """
    uuid.UUID(str(set_id));uuid.UUID(str(source_id))
    if not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError('Source hash')
    ext=Path(filename).suffix.casefold()
    if ext not in ('.docx','.doc','.pdf'):raise ValueError('Source format')
    originals=store.directory/'originals'/sha[:2]
    originals.mkdir(parents=True,exist_ok=True)
    target=originals/(sha+ext)
    with tempfile.NamedTemporaryFile(dir=originals,prefix='.incoming-',delete=False) as tmp:
        temporary=Path(tmp.name);size=0;digest=hashlib.sha256()
        try:
            while True:
                chunk=stream.read(1024*1024)
                if not chunk:break
                size+=len(chunk)
                if size>MAX_FILE:raise ValueError('Source too large')
                tmp.write(chunk);digest.update(chunk)
            tmp.flush();os.fsync(tmp.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True);raise
    try:
        if digest.hexdigest()!=sha or not size:raise ValueError('Source checksum mismatch')
        inspect(temporary,filename)
        if target.exists():
            if sha256(target)!=sha:raise Conflict('Immutable source file collision')
        else:os.replace(temporary,target)
        journal_path=store.directory/'coverage'/f'{source_id}.json'
        if journal_path.exists():
            previous=json.loads(journal_path.read_text(encoding='utf-8'))
            if previous.get('sha256')!=sha or previous.get('set_id')!=str(set_id):raise Conflict('Source journal collision')
            if previous.get('summary',{}).get('parser_version')!=PARSER_VERSION:
                raise Conflict('Parser changed: create a new source revision rather than overwriting evidence')
            return previous['summary']
        result=parse(target,filename)
        old_context=[]
        if supersedes:
            old_path=store.directory/'coverage'/f'{uuid.UUID(str(supersedes))}.json'
            if old_path.exists():
                old=json.loads(old_path.read_text(encoding='utf-8'))
                if old.get('set_id')!=str(set_id):raise Conflict('Revision crosses sets')
                old_context=[b['context_hash'] for b in old.get('blocks',[])]
        source_payload=dict(sha256=sha,original_key=f'originals/{sha[:2]}/{sha+ext}',parser_version=PARSER_VERSION,
                            filename=filename,classification=result['classification'],supersedes=supersedes)
        entries=[(set_id,'source_revision',str(source_id),1,source_payload)]
        for block in result['blocks']:
            fid=str(uuid.uuid5(uuid.UUID(str(source_id)),block['locator']))
            payload=dict(source_revision=[str(source_id),1],locator=block['locator'],exact_text=block['exact_text'],
                         search_text=block['search_text'],context_hash=block['context_hash'],
                         structure={k:v for k,v in block.items() if k not in ('exact_text','search_text','context_hash','locator')})
            entries.append((set_id,'fragment',fid,1,payload))
        store.put_records_batch(entries)
        state_counts={state:sum(x['state']==state for x in result['coverage']) for state in ('context','needs_review','unreadable')}
        issues=[x for x in result['coverage'] if x['state']=='unreadable']
        old_set=set(old_context);new_set={b['context_hash'] for b in result['blocks']}
        reuse={'unchanged_contexts':sum(b['context_hash'] in old_set for b in result['blocks']),
               'invalidated_contexts':sum(context not in new_set for context in old_context)}
        summary=dict(kind='source.ingested',set_id=str(set_id),source_id=str(source_id),sha256=sha,
                    parser_version=PARSER_VERSION,classification=result['classification'],counts=result['counts'],
                    fragment_count=len(result['blocks']),coverage_summary=state_counts,issues=issues[:200],reuse=reuse,
                    coverage_count=len(result['coverage']),
                    coverage_digest=checksum([checksum(result['coverage'][i:i+500]) for i in range(0,len(result['coverage']),500)]))
        journal_path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=journal_path.parent,prefix='.coverage-',delete=False) as output:
            journal_tmp=Path(output.name)
            json.dump(dict(set_id=str(set_id),sha256=sha,summary=summary,coverage=result['coverage'],
                           blocks=[{'locator':b['locator'],'context_hash':b['context_hash']} for b in result['blocks']]),output,ensure_ascii=False)
            output.flush();os.fsync(output.fileno())
        os.replace(journal_tmp,journal_path)
        return summary
    finally:temporary.unlink(missing_ok=True)
