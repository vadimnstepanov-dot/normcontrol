"""Conservative PDF table candidates and explicitly adjacent notes, with geometry."""
import re


def borderless_candidates(page,known):
    def overlap(a,b):
        area=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        return area/max(1,(a[2]-a[0])*(a[3]-a[1]))
    result=[]
    for t in page.find_tables(dict(vertical_strategy='text',horizontal_strategy='text',min_words_vertical=3,min_words_horizontal=2)):
        if any(overlap(t.bbox,k.bbox)>.2 for k in known):continue
        values=t.extract()
        if len(values)<3 or not 2<=max(map(len,values),default=0)<=20:continue
        populated=[row for row in values if any(x and x.strip() for x in row)]
        if len(populated)<3:continue
        if sum(bool(x and x.strip()) for row in populated for x in row)<.6*sum(len(row) for row in populated):continue
        result.append(t)
    return result


def adjacent_notes(page,table,other_tables):
    """Only explicit note labels immediately beneath the same table. No guessed footnote IDs."""
    from .tables import finalize
    words=page.extract_words();left,top,right,bottom=table['bbox']
    x0,x1,y0=left*page.width,right*page.width,bottom*page.height
    limit=min([t['bbox'][1]*page.height for t in other_tables if t['bbox'][1]>bottom]+[page.height])
    selected=[w for w in words if y0<=w['top']<limit and x0-8<=(w['x0']+w['x1'])/2<=x1+8]
    groups={}
    for w in selected:groups.setdefault(round(w['top']/3),[]).append(w)
    lines=[sorted(v,key=lambda x:x['x0']) for _,v in sorted(groups.items())]
    if not lines:return []
    first=' '.join(w['text'] for w in lines[0])
    if min(w['top'] for w in lines[0])-y0>36 or not re.match(r'^Примечани[ея]\b',first,re.I):return []
    kept=[];last=y0
    for i,line in enumerate(lines):
        top=min(w['top'] for w in line);text=' '.join(w['text'] for w in line)
        if i and (top-last>16 or re.match(r'^(?:\d+(?:\.\d+)*\s+[А-ЯA-Z]|Таблица\s|Рисунок\s|Приложение\s)',text)):break
        kept+=line;last=max(w['bottom'] for w in line)
    text='\n'.join(' '.join(w['text'] for w in line) for line in lines if all(w in kept for w in line))
    if not text:return []
    locator=table['id']+'/adjacent-note'
    box=[min(w['x0'] for w in kept)/page.width,min(w['top'] for w in kept)/page.height,
         max(w['x1'] for w in kept)/page.width,max(w['bottom'] for w in kept)/page.height]
    binding=dict(source=locator,exact_text=text,scope='table',basis='explicit_adjacent_note_label',page=page.page_number,bbox=box)
    table.setdefault('note_refs',[]).append(locator)
    for c in table['cells']:c.setdefault('note_bindings',[]).append(binding)
    finalize(table)
    return [dict(locator=locator,kind='table_note',exact_text=text,search_text=text.casefold(),heading_path=[],
                 page=page.page_number,bbox=box,read_method='pdf_text',recognition_confidence=None)]


def borderless_table(page,detected,index):
    """Drop detector-created whitespace rows and explicit outside captions/notes.

    Native word blocks remain complete. This is a separate unapproved topology
    candidate, never permission to discard source paragraphs or blank ruled cells.
    """
    from .tables import cell,finalize
    from .structure_pdf import text_table
    raw=text_table(page,detected,index);groups={}
    for c in raw['cells']:groups.setdefault(c['row'],[]).append(c)
    rows=[];caption=''
    for _,cs in sorted(groups.items()):
        cs.sort(key=lambda c:c['column']);text=' '.join(c['exact_text'] for c in cs).strip()
        if not text:continue
        if not rows and re.match(r'^(?:Продолжение\s+|Окончание\s+)?таблиц',text,re.I):caption=text;continue
        if re.match(r'^Примечани[ея]\b',text,re.I):break
        if not rows and sum(bool(c['exact_text'].strip()) for c in cs)<2:continue
        rows.append(cs)
    if len(rows)<3:return None
    cs=[]
    for r,old in enumerate(rows,1):
        for c in old:
            cs.append(cell(f'{raw["id"]}/r{r}/c{c["column"]}',r,c['column'],c['exact_text'],
                           colspan=c['colspan'],method='pdfplumber',bbox=c['bbox']))
    return finalize(dict(raw,cells=cs,rows=len(rows),header_rows=[1],caption=caption or raw['caption'],
        issues=['borderless_structure_candidate'],detection='text_alignment',
        bbox=[min(c['bbox'][0] for c in cs),min(c['bbox'][1] for c in cs),max(c['bbox'][2] for c in cs),max(c['bbox'][3] for c in cs)],
        detector_bbox=raw['bbox'],detector_row_count=raw['rows']))
