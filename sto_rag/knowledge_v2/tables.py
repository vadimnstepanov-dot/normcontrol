"""Loss-aware table IR. Geometry checks never imply normative/semantic approval."""
import re
from decimal import Decimal, InvalidOperation
from .store import checksum

TABLE_SCHEMA = 'table-ir/1'


def constraints(text):
    """Keep literals as evidence; do not interpret dash, blank or yes/no as each other."""
    values = []
    for m in re.finditer(r'(?P<op>≤|≥|<=|>=|<|>|=|не более|не менее)?\s*(?P<n>[−-]?\d+(?:[.,]\d+)?)\s*(?P<unit>%|мс|сек\.?|с|час(?:а|ов)?|ч|мм|ГБ|МБ|кбит/с|Мбит/с)?', text, re.I):
        try: number = str(Decimal(m['n'].replace(',', '.').replace('−', '-')))
        except InvalidOperation: continue
        values.append(dict(literal=m.group().strip(), start=m.start(), end=m.end(),
                           number=number, operator=m['op'] or '', unit=m['unit'] or ''))
    return dict(numbers=values, negatives=re.findall(r'\b(?:не|нет|запрещено)\b', text, re.I),
                blank=text == '', dash=text.strip() in ('-', '–', '—'),
                not_applicable=bool(re.search(r'не применим[оаы]?|н/п', text, re.I)))


def cell(cid, row, column, text, *, rowspan=1, colspan=1, method='ooxml', bbox=None, confidence=None):
    return dict(id=cid, row=row, column=column, rowspan=rowspan, colspan=colspan,
                exact_text=text, normalized_text=re.sub(r'\s+', ' ', text).strip(),
                method=method, recognition_confidence=confidence, bbox=bbox,
                constraints=constraints(text), header_refs=[], row_label_refs=[], note_refs=[], issues=[])


def finalize(table):
    """Explicit occupancy including gaps/merges; never silently fill missing values."""
    table['schema_version'] = TABLE_SCHEMA
    issues = list(table.get('issues', [])); occupied = {}; by_id = {}
    rows, cols = table['rows'], table['columns']
    if not 1 <= rows <= 10000 or not 1 <= cols <= 200: raise ValueError('Table dimensions')
    for c in table['cells']:
        if c['id'] in by_id: issues.append('duplicate_cell_id')
        by_id[c['id']] = c
        if any(type(c[k]) is not int or c[k] < 1 for k in ('row', 'column', 'rowspan', 'colspan')):
            raise ValueError('Positive cell coordinates required')
        if c['row']+c['rowspan']-1>rows or c['column']+c['colspan']-1>cols:
            issues.append('cell_outside_grid'); continue
        for r in range(c['row'], c['row']+c['rowspan']):
            for col in range(c['column'], c['column']+c['colspan']):
                if (r,col) in occupied: issues.append('overlapping_cells')
                occupied[r,col]=c['id']
    table['missing_slots'] = [[r,c] for r in range(1,rows+1) for c in range(1,cols+1) if (r,c) not in occupied]
    if table['missing_slots']: issues.append('grid_gaps')
    header_rows = table.get('header_rows', [])
    interpreted=table.get('header_basis')=='model_structure' and bool(table.get('structural_interpretation'))
    geometric=table.get('header_basis')=='source_geometry' and table.get('structural_rule') in ('single_cell_container','empty_text_grid')
    headerless=(interpreted or geometric) and table.get('table_role') in ('key_value','glossary','contents','layout','text_block')
    if not header_rows and not headerless: issues.append('header_unknown')
    if table.get('header_basis') not in ('ooxml_explicit', 'expert') and not interpreted and not geometric: issues.append('header_inferred')
    table['structural_review_basis']='model_candidate' if interpreted else table.get('header_basis','unknown')
    widths=table.get('grid_widths')
    if not widths or len(widths)!=cols or any(w<=0 for w in widths):widths=[1]*cols
    edges=[0]
    for width in widths:edges.append(edges[-1]+width)
    def interval(c):return edges[c['column']-1],edges[min(cols,c['column']+c['colspan']-1)]
    for c in table['cells']:
        left,right=interval(c);candidates=[]
        applicable_headers=header_rows
        if table.get('repeated_header_rows'):
            preceding=[r for r in table['repeated_header_rows'] if r<c['row']]
            applicable_headers=[max(preceding)] if preceding else []
        for h in table['cells']:
            if h['row'] not in applicable_headers or h['row']>=c['row']:continue
            hl,hr=interval(h);share=max(0,min(right,hr)-max(left,hl))/max(1,right-left)
            if share:candidates.append(dict(ref=h['id'],share=round(share,5)))
        # Word grids can contain tiny layout slivers from slightly misaligned borders.
        # Keep them as evidence, but don't attach a value to three semantic headers.
        c['header_overlap']=candidates
        c['header_refs']=[h['ref'] for h in candidates if h['share']>=.15]
        c['header_sliver_refs']=[h['ref'] for h in candidates if h['share']<.15]
        groups=[h for h in table['cells'] if h['column']==1 and h['colspan']==cols and h['row']<c['row'] and h['row'] not in header_rows]
        c['row_group_refs']=[groups[-1]['id']] if groups else []
        c['row_label_refs'] = [h['id'] for h in table['cells'] if h['column'] < c['column']
                               and h['row'] <= c['row'] < h['row']+h['rowspan'] and h['row'] not in header_rows]
        c['note_refs'] = list(dict.fromkeys(c.get('note_refs', [])+table.get('note_refs', [])))
    table['issues'] = sorted(set(issues))
    table['structure_status'] = 'pass' if not table['issues'] else 'needs_review'
    # Source exactness and topology are necessary, not sufficient for expert approval.
    table['confirmed_conclusions_allowed'] = False
    table['digest'] = checksum({k:v for k,v in table.items() if k!='digest'})
    return table


def compare_tables(primary, alternative):
    """Report disagreements at cell anchors, including numerals/operators/headers."""
    fields=('rowspan','colspan','exact_text')
    a={(c['row'],c['column']):c for c in primary['cells']}
    b={(c['row'],c['column']):c for c in alternative['cells']}
    diffs=[]
    for key in sorted(set(a)|set(b)):
        x,y=a.get(key),b.get(key)
        if x is None or y is None: diffs.append(dict(at=list(key),kind='missing_cell')); continue
        changed=['exact_text'] if re.sub(r'\s+',' ',x['exact_text']).strip()!=re.sub(r'\s+',' ',y['exact_text']).strip() else []
        changed += [k for k in ('rowspan','colspan') if x[k]!=y[k]]
        if changed:
            def critical(c):
                q=constraints(c['exact_text'])
                return [[n['number'],n['operator'],n['unit']] for n in q['numbers']],q['negatives'],q['blank'],q['dash'],q['not_applicable']
            diffs.append(dict(at=list(key),fields=changed,primary=x['exact_text'],alternative=y['exact_text'],
                              critical=critical(x)!=critical(y)))
    if primary['rows']!=alternative['rows'] or primary['columns']!=alternative['columns']:
        diffs.append(dict(kind='dimensions_mismatch',critical=True))
    if primary.get('header_rows') != alternative.get('header_rows'):
        diffs.append(dict(kind='header_rows_mismatch',critical=True))
    return diffs


def continuations(tables):
    """Conservative evidence-backed links, never overwrite physical tables or cells."""
    # May run again after model-assisted header verification. Remove only notes
    # previously inherited by this function before recomputing the proven chain.
    for t in tables:
        inherited=t.pop('continuation_note_refs',[])
        t['note_refs']=[r for r in t.get('note_refs',[]) if r not in inherited]
        for c in t['cells']:
            added=c.pop('continuation_note_refs',[])
            c['note_refs']=[r for r in c.get('note_refs',[]) if r not in added]
        t.pop('continuation_of',None);t.pop('continuation_evidence',None)
    for previous,current in zip(tables,tables[1:]):
        caption=current.get('caption','')
        if not re.search(r'продолжение|окончание',caption,re.I): continue
        def number(t):
            m=re.search(r'таблиц\w*\s+([А-ЯA-Z]?\.?\d+(?:\.\d+)*)',t.get('caption',''),re.I)
            return m.group(1) if m else None
        def headers(t):
            widths=t.get('grid_widths') or [1]*t['columns'];total=sum(widths)
            if len(widths)!=t['columns'] or total<=0:widths=[1]*t['columns'];total=t['columns']
            rows=t.get('header_rows',[]);base=min(rows) if rows else 0
            result=[]
            for c in t['cells']:
                if c['row'] not in rows:continue
                text=re.sub(r'\s+','',c['exact_text']).replace('\u00ad','').casefold()
                result.append((c['row']-base,c['rowspan'],text,sum(widths[:c['column']-1])/total,
                               sum(widths[:c['column']-1+c['colspan']])/total))
            return result
        a,b=headers(previous),headers(current)
        aligned=bool(a and len(a)==len(b) and all(x[:3]==y[:3] and abs(x[3]-y[3])<.015 and abs(x[4]-y[4])<.015 for x,y in zip(a,b)))
        same=(number(current) and number(current)==number(previous) and aligned
              and current.get('heading_path',[])==previous.get('heading_path',[]))
        if current.get('page') and previous.get('page'):
            same=bool(same and current['page']==previous['page']+1)
        if same:
            current['continuation_of']=previous['id']
            current['continuation_evidence']=['explicit_caption','aligned_physical_header_intervals','identical_header_hierarchy','same_context']
            current['issues']=[x for x in current.get('issues',[]) if x!='unresolved_continuation']
            inherited=[r for r in previous.get('note_refs',[]) if r not in current.get('note_refs',[])]
            current['continuation_note_refs']=inherited
            current['note_refs']=list(dict.fromkeys(current.get('note_refs',[])+inherited))
            for c in current['cells']:
                c['continuation_note_refs']=[r for r in inherited if r not in c.get('note_refs',[])]
        else:
            current.setdefault('issues',[]).append('unresolved_continuation')
        finalize(current)


def row_bundles(table, max_chars=14000):
    """Each value anchor once; repeat only required headers, notes and spanning labels."""
    headers=[c for c in table['cells'] if c['row'] in table.get('header_rows',[])+table.get('preamble_rows',[])]
    data=[c for c in table['cells'] if c not in headers]; current=[]; length=0
    overhead=sum(len(c['exact_text']) for c in headers)
    def bundle(items):
        start=min(c['row'] for c in items);end=max(c['row'] for c in items)
        refs={x for c in items for key in ('row_label_refs','row_group_refs') for x in c.get(key,[])}
        context=[c for c in data if c not in items and (c['id'] in refs or c['row']<start<c['row']+c['rowspan'])]
        evidence=items+headers+context
        notes=list(dict.fromkeys(table.get('note_refs',[])+[n for c in evidence for n in c.get('note_refs',[])]))
        bindings={checksum(b):b for c in evidence for b in c.get('note_bindings',[])}
        chars=sum(len(c['exact_text']) for c in evidence)+sum(len(b['exact_text']) for b in bindings.values())
        return dict(table_id=table['id'],row_range=[start,end],cells=items,headers=headers,context_cells=context,
                    note_refs=notes,note_bindings=list(bindings.values()),oversized=chars>max_chars)
    for r in sorted({c['row'] for c in data}):
        row=[c for c in data if c['row']==r]; size=sum(len(c['exact_text']) for c in row)
        if current and length+size+overhead>max_chars:
            yield bundle(current);current=[];length=0
        current+=row;length+=size
    if current: yield bundle(current)
