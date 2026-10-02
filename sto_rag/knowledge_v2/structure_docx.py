"""OOXML topology and assets; exact source text is never replaced with OCR."""
import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from word_source import open_archive as ZipFile
from .ingest import W, parse_docx, element_text, normalize
from .numbering import Numbering
from .tables import cell, finalize, continuations

R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def bind_preceding_table_notes(blocks, tables):
    """Bind a source legend before a caption, within the same local section only."""
    paragraphs=sorted((b for b in blocks if b.get('kind')=='paragraph' and 'document_position' in b),
                      key=lambda b:b['document_position'])
    for t in tables:
        position=t.get('document_position')
        if position is None or not t.get('caption'):continue
        lower=max((x['document_position'] for x in tables if x.get('document_position',position)>=0
                   and x.get('document_position',position)<position),default=-1)
        nearby=[b for b in paragraphs if lower<b['document_position']<position]
        for b in reversed(nearby):
            text=b['exact_text'].strip()
            if not text:continue
            if b.get('is_heading') or b.get('heading_refs',[])!=t.get('heading_refs',[]):break
            if re.match(r'^(?:(?:Продолжение|Окончание)\s+)?Таблиц[аы]\s+',text,re.I):continue
            if not re.match(r'^(?:Примечани[ея]|Легенда|Условные обозначения)\b',text,re.I):break
            # A nearby generic note alone cannot establish scope. Require explicit
            # table language and at least two literal legend values in this table.
            quoted=re.findall(r'«([^»]+)»|"([^"]+)"',text)
            values={re.sub(r'\s+','',a or c).casefold() for a,c in quoted}
            cells={re.sub(r'\s+','',c['exact_text']).casefold() for c in t['cells']}
            matched=sorted(v for v in values if v and any(s==v or re.fullmatch(re.escape(v)+r'\d+\)',s) for s in cells))
            if not re.search(r'таблиц',text,re.I) or len(matched)<2:continue
            # An explicit reference to another table overrides mere proximity.
            number=re.search(r'таблиц\w*\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)',t['caption'],re.I)
            refs=re.findall(r'таблиц\w*\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)',text,re.I)
            if refs and (not number or any(ref!=number[1] for ref in refs)):continue
            t['note_refs']=list(dict.fromkeys(t.get('note_refs',[])+[b['locator']]))
            binding=dict(source=b['locator'],basis='preceding_legend_same_section_literal_values',values=matched)
            if binding not in t.setdefault('legend_bindings',[]):t['legend_bindings'].append(binding)


def read_docx(path, assets):
    blocks,old_coverage,counts=parse_docx(path)
    issues=[x for x in old_coverage if not any(s in x['reason'] for s in ('numbering','number','Visual object','Nested table','Empty table','Embedded object'))]
    original={b['locator']:b for b in blocks};tables=[];visuals=[]
    with ZipFile(path) as z:
        root=ET.fromstring(z.read('word/document.xml'));body=root.find(W+'body')
        nums=Numbering(ET.fromstring(z.read('word/numbering.xml')) if 'word/numbering.xml' in z.namelist() else None)
        styles={s.get(W+'styleId'):s for s in ET.fromstring(z.read('word/styles.xml'))} if 'word/styles.xml' in z.namelist() else {}
        def properties(node):
            ps=node.find(f'{W}pPr/{W}pStyle');sid=ps.get(W+'val') if ps is not None else None
            chain=[];seen=set();props={}
            while sid in styles and sid not in seen:
                seen.add(sid);style=styles[sid];chain.insert(0,style.find(W+'pPr'))
                parent=style.find(W+'basedOn');sid=parent.get(W+'val') if parent is not None else None
            chain.append(node.find(W+'pPr'))
            for pr in chain:
                if pr is None:continue
                for key in ('numId','ilvl'):
                    item=pr.find(f'{W}numPr/{W}{key}')
                    if item is not None:props['num' if key=='numId' else key]=item.get(W+'val')
            return props
        # Counters follow all body paragraphs, including table cells and empty list items.
        labels={id(node):nums.label(properties(node)) for node in body.iter(W+'p')}
        rels={}
        if 'word/_rels/document.xml.rels' in z.namelist():
            for r in ET.fromstring(z.read('word/_rels/document.xml.rels')):
                rels[r.get('Id')]=(r.get('Target',''),r.get('TargetMode','Internal'),r.get('Type',''))
        def assets_for(node,locator):
            references={v for x in node.iter() for k,v in x.attrib.items() if k in (R+'embed',R+'id',R+'link')}
            previews={}
            for container in node.iter(W+'object'):
                embedded=[x.get(R+'id') for x in container.iter() if x.tag.endswith('OLEObject')]
                images=[x.get(R+'id') for x in container.iter() if x.tag.endswith('imagedata')]
                if len(embedded)==1:
                    for rid in images:previews[rid]=locator+'/'+embedded[0]
            for rid in sorted(references):
                target,mode,reltype=rels.get(rid,('','',''))
                if mode=='External':
                    if not reltype.endswith('/hyperlink'):
                        issues.append(dict(locator=locator,state='unreadable',reason='External relationship not fetched'))
                    continue
                member=posixpath.normpath(posixpath.join('word',target))
                if not member.startswith(('word/media/','word/embeddings/')) or member not in z.namelist():continue
                data=z.read(member);h=hashlib.sha256(data).hexdigest();ext=Path(member).suffix.lower()
                filename=h+ext;destination=assets/filename
                if not destination.exists():destination.write_bytes(data)
                visual=dict(id=locator+'/'+rid,locator=locator,original='assets/'+filename,sha256=h,
                            member=member,kind='embedded_object' if '/embeddings/' in member else 'image',
                            state='pending',method=None,issues=[])
                visual['progid']=next((x.get('ProgID','') for x in node.iter() if x.get(R+'id')==rid and x.tag.endswith('OLEObject')),'')
                if rid in previews:visual['preview_of']=previews[rid]
                if visual['kind']=='embedded_object':
                    visual.update(state='unreadable',issues=['Embedded object retained; no execution; preview does not prove internal content'])
                visuals.append(visual)
        def footnotes(node):
            return [x.tag.split('}')[-1].replace('Reference','')+'s/'+x.get(W+'id','') for x in node.iter()
                    if x.tag in (W+'footnoteReference',W+'endnoteReference')]
        def build_table(node,tid,context,caption=''):
            rows=node.findall(W+'tr');anchors=[];vertical={};maxcol=0;headers=[];table_issues=[];source_slots=[]
            grid=node.findall(f'{W}tblGrid/{W}gridCol');grid_widths=[int(x.get(W+'w','0')) for x in grid]
            for ri,row in enumerate(rows,1):
                before=row.find(f'{W}trPr/{W}gridBefore');column=1+int(before.get(W+'val','0')) if before is not None else 1
                active={}
                header=row.find(f'{W}trPr/{W}tblHeader')
                if header is not None and header.get(W+'val','1') not in ('0','false','off'):headers.append(ri)
                for ci,tc in enumerate(row.findall(W+'tc'),1):
                    loc=f'{tid}/r{ri}/c{column}';span=tc.find(f'{W}tcPr/{W}gridSpan')
                    width=int(span.get(W+'val','1')) if span is not None else 1
                    if width<1 or width>200:raise ValueError('Invalid gridSpan')
                    vm=tc.find(f'{W}tcPr/{W}vMerge');mode=vm.get(W+'val','continue') if vm is not None else None
                    text='\n'.join(element_text(p) for p in tc.findall(W+'p')).strip()
                    origin=vertical.get((column,width)) if mode=='continue' else None
                    if mode=='continue' and origin:
                        origin['rowspan']+=1;active[column,width]=origin
                        if text and text!=origin['exact_text']:
                            table_issues.append('vertical_continuation_has_text')
                            origin.setdefault('continuation_texts',[]).append(dict(locator=loc,text=text))
                        source_slots.append(dict(locator=loc,origin=origin['id'],raw_text=text))
                    else:
                        c=cell(loc,ri,column,text,colspan=width)
                        c['source_locator']=dict(part='word/document.xml',table=tid,row=ri,xml_cell=ci)
                        c['note_refs']=footnotes(tc);anchors.append(c)
                        c['paragraph_units']=[dict(index=i,exact_text=element_text(p),number_label=labels[id(p)][0],
                            numbering_issue=labels[id(p)][1]) for i,p in enumerate(tc.findall(W+'p'),1)]
                        raised=' '.join(element_text(run) for run in tc.findall('.//'+W+'r')
                            if run.find(f'{W}rPr/{W}vertAlign[@{W}val="superscript"]') is not None or run.find(f'{W}rPr/{W}position') is not None)
                        c['note_markers']=re.findall(r'(\d+)\)',raised)
                        if mode=='continue':c['issues'].append('orphan_vertical_merge');table_issues.append('orphan_vertical_merge')
                        if mode=='restart':active[column,width]=c
                    assets_for(tc,loc)
                    for ni,nested in enumerate(tc.findall(W+'tbl'),1):
                        build_table(nested,loc+f'/t{ni}',context)
                    column+=width
                maxcol=max(maxcol,column-1);vertical=active
            if not headers:
                # Candidate only; do not silently claim a first data row is a verified header.
                headers=[1];basis='first_row_candidate'
            else:basis='ooxml_explicit'
            t=dict(id=tid,rows=len(rows),columns=max(maxcol,len(grid)),cells=anchors,header_rows=headers,
                   header_basis=basis,caption=caption,heading_path=context.get('heading_path',[]),
                   heading_refs=context.get('heading_refs',[]),grid_widths=grid_widths,
                   source_slots=source_slots,note_refs=[],issues=table_issues,method='ooxml',page=None)
            tables.append(finalize(t));return t
        p=0;ti=0;heading_context={};caption='';last_table=None;numbered={}
        for position,child in enumerate(body):
            if child.tag==W+'p':
                p+=1;loc=f'p{p}';b=original.get(loc)
                if b:
                    b['document_position']=position
                    label,error=labels[id(child)]
                    b.update(number_label=label,numbering_verified=False,numbering_method='ooxml_counter_unverified',
                             source_locator=dict(part='word/document.xml',paragraph=p),note_refs=footnotes(child))
                    if error:issues.append(dict(locator=loc+'/numbering',state='needs_review',reason=error))
                    if label:numbered[loc]=label
                    if b.get('is_heading'):heading_context=b
                    raw=b['exact_text']
                    if re.match(r'^(?:(?:Продолжение|Окончание)\s+)?Таблиц[аы]\s+',raw,re.I):caption=raw
                    if last_table and re.match(r'^(?:Примечани[ея]|\*|Легенда)\b',raw,re.I):
                        last_table['note_refs'].append(loc)
                    elif raw.strip() and not re.match(r'^(?:Примечани[ея]|\*|Легенда)\b',raw,re.I):last_table=None
                assets_for(child,loc)
            elif child.tag==W+'tbl':
                previous=last_table
                ti+=1;last_table=build_table(child,f't{ti}',heading_context,caption);last_table['document_position']=position;caption=''
                if previous and previous.get('document_position')==position-1 and not last_table['caption']:
                    last_table['adjacent_caption_context']=previous.get('caption') or previous.get('adjacent_caption_context','')
                    last_table['adjacent_caption_source']=previous['id']
        def table_family(t):
            match=re.search(r'Таблиц[аы]\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)',t.get('caption') or t.get('adjacent_caption_context',''),re.I)
            return (tuple(t.get('heading_refs',[])),match.group(1)) if match else None
        for t in tables:
            for c in t['cells']:
                for marker in c.get('note_markers',[]):
                    candidates=[b for b in blocks if b['kind']=='paragraph' and b.get('document_position',-1)>t.get('document_position',10**9)
                        and b.get('heading_refs',[])==t.get('heading_refs',[])
                        and re.match(r'^\s*'+re.escape(marker)+r'\)\s*\S',b['exact_text'])]
                    cell_candidates=[]
                    family=table_family(t)
                    if family:
                        for related in tables:
                            if table_family(related)!=family:continue
                            for note_cell in related['cells']:
                                if note_cell['colspan']!=related['columns']:continue
                                units=note_cell.get('paragraph_units',[])
                                for i,u in enumerate(units):
                                    if u['number_label']==marker+')' and not u['numbering_issue']:
                                        end=next((j for j in range(i+1,len(units)) if units[j]['number_label']),len(units))
                                        cell_candidates.append(dict(marker=marker,source=note_cell['id'],paragraph_start=u['index'],
                                            paragraph_end=units[end-1]['index'],exact_text='\n'.join(x['exact_text'] for x in units[i:end]),
                                            basis='same_caption_and_heading_numbered_full_width_cell'))
                    if len(candidates)+len(cell_candidates)==1:
                        if candidates:c['note_refs'].append(candidates[0]['locator'])
                        else:
                            binding=cell_candidates[0];c['note_refs'].append(binding['source'])
                            c.setdefault('note_bindings',[]).append(binding)
                    else:t['issues'].append('unresolved_note_marker:'+marker)
                c['note_refs']=list(dict.fromkeys(c['note_refs']))
        if numbered:
            issues.append(dict(locator='document/numbering',state='needs_review',reason='Computed Word list labels require rendered verification before use as clause addresses'))
        # Notes/revisions from the legacy parser remain distinct evidence.
        for b in blocks:
            b['heading_addresses']=[(numbered.get(ref,'')+' '+original.get(ref,{}).get('exact_text','')).strip()
                                    for ref in b.get('heading_refs',[])]
        bind_preceding_table_notes(blocks,tables)
        continuations(tables)
        blocks=[b for b in blocks if b['kind']!='table_cell']
        for t in tables:
            finalize(t);by={c['id']:c for c in t['cells']}
            for c in t['cells']:
                blocks.append(dict(locator=c['id'],kind='table_cell',exact_text=c['exact_text'],search_text=normalize(c['exact_text']).casefold(),
                    heading_path=t['heading_path'],heading_refs=t['heading_refs'],table=t['id'],row=c['row'],column=c['column'],
                    span=c['colspan'],rowspan=c['rowspan'],table_label=t['caption'],header_path=[by[x]['exact_text'] for x in c['header_refs']],
                    header_refs=c['header_refs'],row_label_refs=c['row_label_refs'],row_group_refs=c.get('row_group_refs',[]),note_refs=c['note_refs'],
                    source_locator=c['source_locator'],read_method='ooxml',recognition_confidence=None,
                    structure_issues=t['issues']+c['issues']))
    # Preserve document order for downstream contextual extraction; nested tables stay under their parent.
    order={};idx=0;p=0;ti=0
    for child in body:
        if child.tag==W+'p':p+=1;order[f'p{p}']=idx
        elif child.tag==W+'tbl':ti+=1;order[f't{ti}']=idx
        idx+=1
    def sort_key(b):
        parts=re.split(r'(\d+)',b['locator']);natural=tuple((0,int(s)) if s.isdigit() else (1,s) for s in parts)
        return order.get(b['locator'].split('/')[0],len(order)),natural
    blocks.sort(key=sort_key)
    return dict(blocks=blocks,tables=tables,visuals=visuals,coverage=issues,counts=counts)
